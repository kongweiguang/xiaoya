"""同步推理用例通过端口隔离模型库，调度线程由外层处理。"""

from collections.abc import Iterator
from threading import Event
from typing import BinaryIO, Protocol

from local_speech.domain import SpeechStyle, Transcription


class SpeechModels(Protocol):
    """应用层只依赖音频和文本契约，允许替换实际模型实现。"""

    def transcribe(
        self, audio: BinaryIO, language: str | None, *, cancelled: Event
    ) -> Transcription:
        """暂存文件按帧解码而非复制整段上传，取消在模型块边界生效。"""
        ...

    def synthesize_stream(
        self,
        text: str,
        *,
        style: SpeechStyle = SpeechStyle.NEUTRAL,
        cancelled: Event | None = None,
    ) -> Iterator[bytes]:
        """取消信号可撤销排队任务，已启动模型由实现安全收尾，输出保持固定 PCM 契约。"""
        ...

    def close(self) -> None:
        """加速实现可能持有子进程，服务关闭必须释放它们而不依赖垃圾回收时机。"""
        ...


class RecognitionStream(Protocol):
    """每条流持有独立解码缓存，结束只冲刷本条流，不能污染下一位说话者。"""

    def accept(self, pcm: bytes, *, final: bool = False) -> str:
        """输入固定 16kHz 单声道 PCM16；final 补足右侧上下文后返回完整文本。"""
        ...


class StreamingRecognizer(Protocol):
    """模型权重在进程内共享，缓存由一次识别流独立持有。"""

    def create_stream(self) -> RecognitionStream:
        """外层可以按 VAD 分段创建缓存，业务层无需认识 ONNX 对象。"""
        ...


class SpeechService:
    """将业务参数约束放在用例附近，模型端口负责执行推理。"""

    def __init__(self, models: SpeechModels, streaming_recognizer: StreamingRecognizer) -> None:
        """流式识别是当前部署的必需能力，缺失应在装配时暴露。"""
        self._models = models
        self._streaming_recognizer = streaming_recognizer

    def create_recognition_stream(self) -> RecognitionStream:
        """一次发声一份缓存，权重共享但不得复用说话者的解码状态。"""
        return self._streaming_recognizer.create_stream()

    def close(self) -> None:
        """生命周期结束后统一委托模型释放资源，应用层不认识 CUDA 或引擎对象。"""
        self._models.close()

    def transcribe(
        self, audio: BinaryIO, language: str | None, *, cancelled: Event
    ) -> Transcription:
        """只探测首字节并恢复文件位置，保持空上传校验而不复制整段音频。"""
        position = audio.tell()
        nonempty = audio.read(1)
        audio.seek(position)
        if not nonempty:
            raise ValueError("音频不能为空")
        return self._models.transcribe(audio, language, cancelled=cancelled)

    def synthesize_stream(
        self,
        text: str,
        *,
        instructions: str | None = None,
        cancelled: Event | None = None,
    ) -> Iterator[bytes]:
        """参数先校验，取消只透传标准库信号，不让应用层依赖 HTTP 或工作线程实现。"""
        if not text.strip():
            raise ValueError("合成文本不能为空")
        style = SpeechStyle.from_instructions(instructions)
        return self._models.synthesize_stream(text, style=style, cancelled=cancelled)
