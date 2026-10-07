"""同步推理用例通过端口隔离模型库，调度线程由外层处理。"""

from collections.abc import Iterator
from threading import Event
from typing import Protocol

from local_speech.domain import SpeechAudio, SpeechStyle, Transcription


class SpeechModels(Protocol):
    """应用层只依赖音频和文本契约，允许替换实际模型实现。"""

    def transcribe(self, audio: bytes, language: str | None) -> Transcription:
        """音频解码与识别策略由实现负责，空识别结果必须保留。"""
        ...

    def synthesize(
        self, text: str, speed: float, *, style: SpeechStyle = SpeechStyle.NEUTRAL
    ) -> SpeechAudio:
        """语气使用领域枚举而非供应商提示，完整音频仍须携带可解码容器。"""
        ...

    def synthesize_stream(
        self,
        text: str,
        speed: float,
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

    def __init__(
        self, models: SpeechModels, streaming_recognizer: StreamingRecognizer | None = None
    ) -> None:
        """流式模型显式注入，未部署时拒绝该协议而不偷偷重算整段音频。"""
        self._models = models
        self._streaming_recognizer = streaming_recognizer

    def create_recognition_stream(self) -> RecognitionStream:
        """一次发声一份缓存；缺模型属于部署错误，不能报告流式接口可用。"""
        if self._streaming_recognizer is None:
            raise ValueError("未部署流式识别模型")
        return self._streaming_recognizer.create_stream()

    def close(self) -> None:
        """生命周期结束后统一委托模型释放资源，应用层不认识 CUDA 或引擎对象。"""
        self._models.close()

    def transcribe(self, audio: bytes, language: str | None) -> Transcription:
        """拒绝空上传；语言可由识别模型自动检测，不强行限制部署场景。"""
        if not audio:
            raise ValueError("音频不能为空")
        return self._models.transcribe(audio, language)

    def synthesize(
        self, text: str, speed: float, *, instructions: str | None = None
    ) -> SpeechAudio:
        """固定模板独立于数值语速；先完成校验，避免未知语气被静默忽略。"""
        if not text.strip():
            raise ValueError("合成文本不能为空")
        if speed != 1.0:
            raise ValueError("当前 CosyVoice 流式部署仅支持 speed=1.0")
        style = SpeechStyle.from_instructions(instructions)
        return self._models.synthesize(text, speed, style=style)

    def synthesize_stream(
        self,
        text: str,
        speed: float,
        *,
        instructions: str | None = None,
        cancelled: Event | None = None,
    ) -> Iterator[bytes]:
        """参数先校验，取消只透传标准库信号，不让应用层依赖 HTTP 或工作线程实现。"""
        if not text.strip():
            raise ValueError("合成文本不能为空")
        if speed != 1.0:
            raise ValueError("当前 CosyVoice 流式部署仅支持 speed=1.0")
        style = SpeechStyle.from_instructions(instructions)
        return self._models.synthesize_stream(text, speed, style=style, cancelled=cancelled)
