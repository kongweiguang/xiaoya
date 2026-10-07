"""OpenAI 音频协议入口不承担推理与模型加载职责。"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Event
from typing import Annotated

from anyio import CancelScope
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from local_speech.bootstrap import create_service

STT_MODEL = "paraformer-streaming"
TTS_MODEL = "cosyvoice3-0.5b"
TTS_VOICE = "default"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """真实预热完成才接单，退出时在线程中关闭模型子进程，避免事件循环阻塞。"""
    app.state.service = await asyncio.to_thread(create_service)
    try:
        yield
    finally:
        await asyncio.to_thread(app.state.service.close)


app = FastAPI(title="xiaoya 本地语音服务", lifespan=lifespan)


class SpeechRequest(BaseModel):
    """保留 SDK 协议兼容性，但语气模板须交给用例校验，不再静默忽略 instructions。"""

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


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: Annotated[UploadFile, File()],
    model: Annotated[str, Form()],
    language: Annotated[str | None, Form()] = None,
    response_format: Annotated[str, Form()] = "json",
) -> dict[str, object]:
    """SDK 使用 JSON 转写协议，推理在线程中执行，文件资源在异常时也会关闭。"""
    if model != STT_MODEL:
        raise HTTPException(400, f"仅支持模型 {STT_MODEL}")
    if response_format not in {"json", "verbose_json"}:
        raise HTTPException(400, "仅支持 json/verbose_json")
    try:
        audio = await file.read()
        result = await asyncio.to_thread(app.state.service.transcribe, audio, language)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    finally:
        await file.close()
    if response_format == "verbose_json":
        return {"text": result.text, "language": result.language, "duration": result.duration}
    return {"text": result.text}


@app.post("/v1/audio/speech")
async def synthesize(request: SpeechRequest) -> Response:
    """固定语气先校验再创建流，PCM 连接持有独立取消信号，不中断其他请求的推理。"""
    if request.model != TTS_MODEL or request.voice != TTS_VOICE:
        raise HTTPException(400, f"仅支持模型 {TTS_MODEL} 和音色 {TTS_VOICE}")
    if request.response_format not in {"wav", "pcm"}:
        raise HTTPException(400, "当前本地部署仅支持 wav/pcm")
    try:
        if request.response_format == "pcm":
            cancelled = Event()
            chunks = app.state.service.synthesize_stream(
                request.input,
                request.speed,
                instructions=request.instructions,
                cancelled=cancelled,
            )
            return StreamingResponse(_pcm_body(chunks, cancelled), media_type="audio/pcm")
        result = await asyncio.to_thread(
            app.state.service.synthesize,
            request.input,
            request.speed,
            instructions=request.instructions,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return Response(result.content, media_type=result.media_type)


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
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted:
                raise asyncio.CancelledError


@app.websocket("/v1/audio/transcriptions/stream")
async def transcribe_stream(websocket: WebSocket) -> None:
    """固定 PCM 契约与明确 commit 保证缓存边界；推理在线程中执行且连接按顺序消费。"""
    await websocket.accept()
    try:
        config = await websocket.receive_json()
        if (
            config.get("model") != "paraformer-streaming"
            or config.get("sample_rate") != 16000
            or config.get("language", "zh") not in {"zh", "en"}
        ):
            raise ValueError("仅支持 paraformer-streaming、16kHz PCM16 单声道、zh/en")
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
    except (ValueError, json.JSONDecodeError) as error:
        await websocket.send_json({"type": "error", "message": str(error)})
        await websocket.close(code=1008)
    except Exception:
        await websocket.close(code=1011)
        raise
