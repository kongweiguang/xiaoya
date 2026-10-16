"""私有 FunASR PCM WebSocket 协议，段落提交与打断继续由本地 VAD 驱动。"""

import asyncio
import json
import time
from contextlib import AsyncExitStack
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
        """Agent 唯一入口只消费流，批量 HTTP 转写保留在独立 speech 诊断接口。"""
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
        """SDK 重采样后只留一个 VAD 窗口原尾块，正常 EOF 也能提交且不缓存整段音频。"""
        super().__init__(stt=recognizer, conn_options=conn_options, sample_rate=16000)
        self._recognizer = recognizer
        self._language = language
        self._pending_final: asyncio.Future[None] | None = None
        self._speech_end_time: float | None = None
        self._input_samples = 0
        self._input_tail = b""
        self._input_ended = False

    async def _run(self) -> None:
        """握手共用连接预算，持续收音不设总时限；失败或取消均回收已登记资源。"""
        detector = self._recognizer.vad.stream()
        try:
            async with aiohttp.ClientSession() as client:
                async with AsyncExitStack() as cleanup:
                    async with asyncio.timeout(self._conn_options.timeout):
                        socket = await cleanup.enter_async_context(
                            client.ws_connect(
                                self._recognizer.url,
                                headers={"Authorization": f"Bearer {self._recognizer.api_key}"},
                                timeout=aiohttp.ClientWSTimeout(
                                    ws_close=self._conn_options.timeout
                                ),
                            )
                        )
                        await socket.send_json(
                            {
                                "model": self._recognizer.model,
                                "language": str(self._language),
                                "sample_rate": 16000,
                            }
                        )
                        ready = await socket.receive_json()
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
        """Silero flush 会丢不足 512 样本的尾窗，故保留有界原字节供正常 EOF 补齐。"""
        self._input_samples = 0
        self._input_tail = b""
        self._input_ended = False
        reset_input = False
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                detector.flush()
                reset_input = True
            else:
                if frame.sample_rate != 16000 or frame.num_channels != 1:
                    raise ValueError("流式识别只接收 SDK 重采样后的 16kHz 单声道")
                if reset_input:
                    self._input_samples = 0
                    self._input_tail = b""
                    reset_input = False
                raw = frame.data.tobytes()
                if len(raw) != frame.samples_per_channel * 2:
                    raise ValueError("PCM16 帧采样数与字节数不一致")
                self._input_samples += frame.samples_per_channel
                self._input_tail = (self._input_tail + raw)[-1024:]
                detector.push_frame(frame)
        self._input_ended = True
        detector.end_input()

    async def _send_audio(self, detector, socket) -> None:
        """按真实 VAD 样本坐标跨 commit 去重；只有正常 EOF 才补原尾块并等待 final。"""
        speaking = False
        uploaded_until = 0
        previous_inference = 0
        async for event in detector:
            if event.type == vad.VADEventType.INFERENCE_DONE:
                if event.samples_index <= previous_inference:
                    # SDK 显式 flush 会重置坐标；新 epoch 不能被旧去重游标吞掉起音。
                    uploaded_until = 0
                previous_inference = event.samples_index
            if event.type == vad.VADEventType.START_OF_SPEECH:
                speaking = True
                self._event_ch.send_nowait(stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH))
                uploaded_until = await self._send_new_frames(event, socket, uploaded_until)
            elif event.type == vad.VADEventType.INFERENCE_DONE and speaking:
                uploaded_until = await self._send_new_frames(event, socket, uploaded_until)
            elif event.type == vad.VADEventType.END_OF_SPEECH:
                speaking = False
                await self._commit_segment(
                    socket, time.time() - event.silence_duration - event.inference_duration
                )
        if speaking:
            if self._input_ended:
                remaining = self._input_samples - uploaded_until
                if remaining < 0 or remaining * 2 > len(self._input_tail):
                    raise RuntimeError("VAD 尾窗与已上传采样位置不一致")
                if remaining:
                    await socket.send_bytes(self._input_tail[-remaining * 2 :])
            await self._commit_segment(socket, time.time())
        await socket.send_json({"type": "end"})

    async def _send_new_frames(self, event, socket, uploaded_until: int) -> int:
        """前导可跨越前次 END，但同一采样只能进入一次识别缓存，不能靠改文字去重。"""
        raw = b"".join(frame.data.tobytes() for frame in event.frames)
        start = event.samples_index - len(raw) // 2
        if start < 0 or len(raw) % 2:
            raise ValueError("VAD 帧与 PCM16 样本位置不一致")
        offset = max(0, uploaded_until - start)
        if offset * 2 < len(raw):
            await socket.send_bytes(raw[offset * 2 :])
            return event.samples_index
        return uploaded_until

    async def _commit_segment(self, socket, speech_end_time: float) -> None:
        """正常段落和 EOF 共用一次确认，取消或失败不补发 end、不重试已上传音频。"""
        self._speech_end_time = speech_end_time
        self._event_ch.send_nowait(
            stt.SpeechEvent(stt.SpeechEventType.END_OF_SPEECH, speech_end_time=speech_end_time)
        )
        self._pending_final = asyncio.get_running_loop().create_future()
        try:
            await socket.send_json({"type": "commit"})
            await asyncio.wait_for(self._pending_final, self._conn_options.timeout)
        finally:
            self._pending_final = None

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
