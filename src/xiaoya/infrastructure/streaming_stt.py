"""私有 FunASR PCM WebSocket 协议，段落提交与打断继续由本地 VAD 驱动。"""

import asyncio
import json
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from livekit.agents import APIConnectOptions, stt, utils, vad
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr


class LocalStreamingSTT(stt.STT):
    """真实增量缓存输出 interim，最终结果只在 VAD 确认停止后提交。"""

    def __init__(
        self, *, base_url: str, model: str, api_key: str, language: str, vad_model
    ) -> None:
        """URL 和鉴权仅取显式私有配置，不继承 SDK 的 Realtime 云端默认设置。"""
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True, interim_results=True, offline_recognize=False
            )
        )
        url = urlsplit(base_url)
        self.url = urlunsplit(
            (
                "wss" if url.scheme == "https" else "ws",
                url.netloc,
                url.path.rstrip("/") + "/audio/transcriptions/stream",
                "",
                "",
            )
        )
        self.model_name = model
        self.api_key = api_key
        self.language = language
        self.vad = vad_model

    @property
    def model(self) -> str:
        """指标使用服务声明的实际模型名，不能伪装为 Whisper。"""
        return self.model_name

    @property
    def provider(self) -> str:
        """只记录供应商名称，避免私有地址与鉴权字段进入指标。"""
        return "local-funasr"

    async def _recognize_impl(self, buffer, *, language, conn_options):
        """该实例只消费流，批量 HTTP 协议由独立配置的兼容客户端处理。"""
        raise NotImplementedError("流式实例只支持 stream()")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        """每条音频流独立连接和缓存，16kHz 重采样由 SDK 输入通道负责。"""
        return LocalRecognitionStream(
            self, language=language or self.language, conn_options=conn_options
        )


class LocalRecognitionStream(stt.RecognizeStream):
    """只上传 VAD 有声段及其前后上下文，静音时不持续占用识别模型。"""

    def __init__(self, recognizer: LocalStreamingSTT, *, language, conn_options) -> None:
        """SDK 管理取消及输入重采样；一次 commit 对应一次最终文本确认。"""
        super().__init__(stt=recognizer, conn_options=conn_options, sample_rate=16000)
        self._recognizer = recognizer
        self._language = language
        self._pending_final: asyncio.Future[None] | None = None
        self._speech_end_time: float | None = None

    async def _run(self) -> None:
        """发送、接收与 VAD 并行，任一路失败立即取消其他任务并关闭连接。"""
        detector = self._recognizer.vad.stream()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(
                    self._recognizer.url,
                    headers={"Authorization": f"Bearer {self._recognizer.api_key}"},
                    timeout=aiohttp.ClientWSTimeout(ws_close=self._conn_options.timeout),
                ) as socket:
                    await socket.send_json(
                        {
                            "model": self._recognizer.model,
                            "language": str(self._language),
                            "sample_rate": 16000,
                        }
                    )
                    ready = await asyncio.wait_for(
                        socket.receive_json(), self._conn_options.timeout
                    )
                    if ready.get("type") != "ready":
                        raise RuntimeError("私有流式识别服务未就绪")
                    tasks = [
                        asyncio.create_task(self._feed_vad(detector)),
                        asyncio.create_task(self._send_audio(detector, socket)),
                        asyncio.create_task(self._receive_text(socket)),
                    ]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        await utils.aio.cancel_and_wait(*tasks)
        finally:
            await detector.aclose()

    async def _feed_vad(self, detector) -> None:
        """VAD 同步消费重采样后的流，结束标记必须传递以冲刷最后一个有声段。"""
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                detector.flush()
            else:
                detector.push_frame(frame)
        detector.end_input()

    async def _send_audio(self, detector, socket) -> None:
        """START 使用前导缓存，之后只送新块；END 的完整缓存不能重复上传。"""
        speaking = False
        async for event in detector:
            if event.type == vad.VADEventType.START_OF_SPEECH:
                speaking = True
                self._event_ch.send_nowait(stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH))
                for frame in event.frames:
                    await socket.send_bytes(frame.data.tobytes())
            elif event.type == vad.VADEventType.INFERENCE_DONE and speaking:
                for frame in event.frames:
                    await socket.send_bytes(frame.data.tobytes())
            elif event.type == vad.VADEventType.END_OF_SPEECH:
                speaking = False
                self._speech_end_time = (
                    time.time() - event.silence_duration - event.inference_duration
                )
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        stt.SpeechEventType.END_OF_SPEECH, speech_end_time=self._speech_end_time
                    )
                )
                self._pending_final = asyncio.get_running_loop().create_future()
                await socket.send_json({"type": "commit"})
                await asyncio.wait_for(self._pending_final, self._conn_options.timeout)
        await socket.send_json({"type": "end"})

    async def _receive_text(self, socket) -> None:
        """临时文本可以修订，final 必须逐段独立；错误和意外断开不能伪装为空识别。"""
        async for message in socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError("私有识别连接意外关闭")
            event = json.loads(message.data)
            if event["type"] == "done":
                return
            if event["type"] == "error":
                raise RuntimeError("私有流式识别服务拒绝请求")
            if event["type"] not in {"partial", "final"}:
                raise RuntimeError("私有识别服务返回未知事件")
            final = event["type"] == "final"
            if event.get("text"):
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        stt.SpeechEventType.FINAL_TRANSCRIPT
                        if final
                        else stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language=self._language, text=event["text"])],
                        speech_end_time=self._speech_end_time if final else None,
                    )
                )
            if final and self._pending_final is not None:
                self._pending_final.set_result(None)
        raise RuntimeError("私有识别服务在结束确认前断开")
