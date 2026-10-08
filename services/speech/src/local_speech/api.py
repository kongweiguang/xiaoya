"""OpenAI 音频协议入口不承担推理与模型加载职责。"""

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from threading import Event
from typing import Annotated

from anyio import CancelScope
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.types import Receive, Scope, Send

from local_speech.bootstrap import create_service

STT_MODEL = "paraformer-streaming"
TTS_MODEL = "cosyvoice3-0.5b"
TTS_VOICE = "default"
TTS_QUEUE_TIMEOUT = 30.0
STREAM_REQUEST_ERROR = "流式请求参数无效，请检查消息类型、字段和 PCM16 音频"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """真实预热完成才接单，退出时在线程中关闭模型子进程，避免事件循环阻塞。"""
    app.state.service = await asyncio.to_thread(create_service)
    app.state.tts_admission = asyncio.Semaphore(1)
    try:
        yield
    finally:
        await asyncio.to_thread(app.state.service.close)


app = FastAPI(title="xiaoya 本地语音服务", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
    """所有 HTTP 参数错误统一为 400，不返回 Pydantic 的原始输入，避免用户文本或凭据泄漏。"""
    return JSONResponse(status_code=400, content={"detail": "请求参数无效，请检查字段类型和格式"})


class SpeechRequest(BaseModel):
    """插件字段按 JSON 实际类型验证，不能把布尔或字符串语速隐式转换成有效固定参数。"""

    model_config = ConfigDict(strict=True)

    model: str
    input: str
    voice: str
    response_format: str = "wav"
    speed: float = 1.0
    instructions: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    """生命周期完成才会接收探测，因此健康状态代表模型已经加载。"""
    return {"status": "ok", "stt": STT_MODEL, "tts": TTS_MODEL, "voice": TTS_VOICE}


@app.get("/v1/models")
async def models() -> dict[str, object]:
    """返回明确模型 ID，兼容插件预热请求，避免宣称任意模型都可使用。"""
    return {"object": "list", "data": [{"id": name} for name in (STT_MODEL, TTS_MODEL)]}


@app.post("/v1/audio/transcriptions", response_model=None)
async def transcribe(
    request: Request,
    file: Annotated[UploadFile, File()],
    model: Annotated[str, Form()],
    language: Annotated[str | None, Form()] = None,
    response_format: Annotated[str, Form()] = "json",
) -> dict[str, object] | Response:
    """暂存文件借给解码线程；断连先通知取消，线程收尾后才允许关闭底层文件。"""
    try:
        if model != STT_MODEL:
            raise HTTPException(400, f"仅支持模型 {STT_MODEL}")
        if response_format not in {"json", "verbose_json"}:
            raise HTTPException(400, "仅支持 json/verbose_json")
    except HTTPException:
        await file.close()
        raise
    cancelled = Event()
    pending = asyncio.create_task(
        asyncio.to_thread(app.state.service.transcribe, file.file, language, cancelled=cancelled)
    )
    disconnected = asyncio.create_task(_wait_disconnect(request.receive))
    try:
        done, _ = await asyncio.wait({pending, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            cancelled.set()
            return Response(status_code=204)
        result = pending.result()
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    finally:
        cancelled.set()
        disconnected.cancel()
        with suppress(asyncio.CancelledError):
            await disconnected
        with CancelScope(shield=True):
            cleanup = asyncio.create_task(_finish_transcription(pending, file))
            await _wait_cleanup(cleanup)
    if response_format == "verbose_json":
        return {"text": result.text, "language": result.language, "duration": result.duration}
    return {"text": result.text}


@app.post("/v1/audio/speech")
async def synthesize(request: SpeechRequest) -> Response:
    """参数先校验；响应自己持有排队、首块预取与断连清理，成功头不早于音频。"""
    if request.model != TTS_MODEL or request.voice != TTS_VOICE:
        raise HTTPException(400, f"仅支持模型 {TTS_MODEL} 和音色 {TTS_VOICE}")
    if request.response_format not in {"wav", "pcm"}:
        raise HTTPException(400, "当前本地部署仅支持 wav/pcm")
    if request.speed != 1.0:
        raise HTTPException(400, "当前 CosyVoice 流式部署仅支持 speed=1.0")
    cancelled = Event()
    try:
        chunks = app.state.service.synthesize_stream(
            request.input,
            instructions=request.instructions,
            cancelled=cancelled,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return SpeechResponse(chunks, cancelled, app.state.tts_admission, request.response_format)


async def _wait_disconnect(receive: Receive) -> None:
    """响应开始前也持续消费断连事件，避免排队和首块推理只能依赖服务器硬取消。"""
    while (await receive())["type"] != "http.disconnect":
        pass


async def _wait_cleanup(cleanup: asyncio.Task) -> None:
    """清理只有一个执行者；重复任务取消与 AnyIO 取消域均不能提前释放推理许可。"""
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


async def _finish_transcription(pending: asyncio.Task, file: UploadFile) -> None:
    """取消后的推理异常已经失去接收者，仍要回收任务结果并在工作结束后关闭文件。"""
    try:
        with suppress(Exception):
            await pending
    finally:
        await file.close()


class SpeechResponse(Response):
    """一条响应负责一种取消边界，PCM 与 WAV 共享首块、模型和许可生命周期。"""

    def __init__(
        self,
        chunks: Iterator[bytes],
        cancelled: Event,
        admission: asyncio.Semaphore,
        response_format: str,
    ) -> None:
        """只登记惰性迭代器，等待许可之前绝不进入线程池或启动 GPU 推理。"""
        super().__init__()
        self._chunks = chunks
        self._cancelled = cancelled
        self._admission = admission
        self._format = response_format

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """断连监控覆盖响应头之前与流输出期间，清理完成后才结束 ASGI 请求。"""
        disconnected = asyncio.create_task(_wait_disconnect(receive))
        output = asyncio.create_task(self._send_audio(scope, receive, send))
        try:
            done, _ = await asyncio.wait(
                {output, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if output in done:
                output.result()
        finally:
            self._cancelled.set()
            disconnected.cancel()
            if not output.done():
                output.cancel()
            with CancelScope(shield=True):
                cleanup = asyncio.create_task(self._finish(output, disconnected))
                await _wait_cleanup(cleanup)

    async def _finish(self, output: asyncio.Task, disconnected: asyncio.Task) -> None:
        """输出任务的 finally 持有真正的推理清理，不能只取消 Task 然后忘记工作线程。"""
        with suppress(asyncio.CancelledError):
            await disconnected
        with suppress(asyncio.CancelledError):
            await output

    async def _send_audio(self, scope: Scope, receive: Receive, send: Send) -> None:
        """首块前故障仍可返回 503，首块后异常直接断流，避免重复播放已输出的句子。"""
        acquired = False
        body = None
        try:
            try:
                await asyncio.wait_for(self._admission.acquire(), TTS_QUEUE_TIMEOUT)
                acquired = True
                body = _pcm_body(self._chunks, self._cancelled)
                first = await anext(body)
            except Exception:
                response = JSONResponse({"detail": "语音服务暂时不可用，请重试"}, status_code=503)
                await response(scope, receive, send)
                return
            if self._format == "pcm":
                response = StreamingResponse(_prepend_pcm(first, body), media_type="audio/pcm")
                await response.stream_response(send)
            else:
                try:
                    content = await _collect_wav(first, body)
                except Exception:
                    response = JSONResponse(
                        {"detail": "语音服务暂时不可用，请重试"}, status_code=503
                    )
                    await response(scope, receive, send)
                    return
                await Response(content, media_type="audio/wav")(scope, receive, send)
        finally:
            self._cancelled.set()
            try:
                if body is None:
                    await asyncio.to_thread(self._chunks.close)
                else:
                    await body.aclose()
            finally:
                if acquired:
                    self._admission.release()


async def _prepend_pcm(first: bytes, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """首块已在响应头前取得，后续保持原始流式次序，不做回放或额外缓冲。"""
    yield first
    async for chunk in body:
        yield chunk


async def _collect_wav(first: bytes, body: AsyncIterator[bytes]) -> bytes:
    """WAV 仅是相同 PCM 的容器，收集期间仍通过同一可取消生成器推进推理。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(first)
        async for chunk in body:
            wav.writeframes(chunk)
    return output.getvalue()


def _next_pcm_chunk(chunks) -> bytes | None:
    """StopIteration 不可穿过 asyncio Future，以独立哨兵表示迭代结束。"""
    return next(chunks, None)


async def _close_pcm_stream(chunks, pending: asyncio.Task | None) -> None:
    """只由独立清理任务顺序等待 next 与 close，重复取消 HTTP 任务也不能并发关闭。"""
    try:
        if pending is not None:
            await pending
    finally:
        await asyncio.to_thread(chunks.close)


async def _pcm_body(chunks, cancelled: Event | None = None) -> AsyncIterator[bytes]:
    """取消先通知等锁线程，再等在途 next 收尾后关闭，避免并发 close 正在运行的生成器。"""
    cancelled = cancelled if cancelled is not None else Event()
    pending = None
    try:
        while not cancelled.is_set():
            pending = asyncio.create_task(asyncio.to_thread(_next_pcm_chunk, chunks))
            chunk = await asyncio.shield(pending)
            pending = None
            if chunk is None or cancelled.is_set():
                return
            yield chunk
    finally:
        cancelled.set()
        # ASGI 的取消域会反复取消 await；清理必须等待同一工作线程安全退出。
        with CancelScope(shield=True):
            cleanup = asyncio.create_task(_close_pcm_stream(chunks, pending))
            await _wait_cleanup(cleanup)


@app.websocket("/v1/audio/transcriptions/stream")
async def transcribe_stream(websocket: WebSocket) -> None:
    """JSON 结构与实际字段类型先验证，非法消息安全关闭；推理和缓存仍按连接顺序消费。"""
    await websocket.accept()
    try:
        config = await websocket.receive_json()
        if not isinstance(config, dict):
            raise ValueError(STREAM_REQUEST_ERROR)
        sample_rate = config.get("sample_rate")
        language = config.get("language", "zh")
        if (
            config.get("model") != "paraformer-streaming"
            or type(sample_rate) is not int
            or sample_rate != 16000
            or not isinstance(language, str)
            or language not in {"zh", "en"}
        ):
            raise ValueError(STREAM_REQUEST_ERROR)
        stream = await asyncio.to_thread(app.state.service.create_recognition_stream)
        previous = ""
        await websocket.send_json({"type": "ready"})
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if pcm := message.get("bytes"):
                if len(pcm) > 32000:
                    raise ValueError("每个音频块最多一秒")
                text = await asyncio.to_thread(stream.accept, pcm)
                if text and text != previous:
                    await websocket.send_json({"type": "partial", "text": text})
                    previous = text
            elif control := message.get("text"):
                event = json.loads(control)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError(STREAM_REQUEST_ERROR)
                if event.get("type") == "commit":
                    text = await asyncio.to_thread(stream.accept, b"", final=True)
                    await websocket.send_json({"type": "final", "text": text})
                    stream = await asyncio.to_thread(app.state.service.create_recognition_stream)
                    previous = ""
                elif event.get("type") == "end":
                    await websocket.send_json({"type": "done"})
                    await websocket.close()
                    return
                else:
                    raise ValueError("未知流式控制消息")
    except WebSocketDisconnect:
        return
    except ValueError:
        await websocket.send_json({"type": "error", "message": STREAM_REQUEST_ERROR})
        await websocket.close(code=1008)
    except Exception:
        await websocket.close(code=1011)
        raise
