"""结果值对象不感知 HTTP、模型库或部署环境。"""

from dataclasses import dataclass
from enum import StrEnum


class SpeechStyle(StrEnum):
    """仅允许已验证的语气，不把任意模型指令作为可执行的合成配置。"""

    NEUTRAL = "neutral"
    HAPPY = "happy"
    GENTLE = "gentle"

    @classmethod
    def from_instructions(cls, instructions: str | None) -> "SpeechStyle":
        """旧客户端省略指令时保持自然音色，未知模板必须在生成音频前明确拒绝。"""
        if instructions is None or instructions == "":
            return cls.NEUTRAL
        templates = {
            "自然平静地说话。": cls.NEUTRAL,
            "开心自然地说话。": cls.HAPPY,
            "轻柔温和地说话。": cls.GENTLE,
        }
        try:
            return templates[instructions]
        except KeyError as error:
            raise ValueError("instructions 仅支持自然、开心、轻柔的固定模板") from error


@dataclass(frozen=True, slots=True)
class Transcription:
    """空文本表示静音或未识别到语音，不能虚构识别结果。"""

    text: str
    language: str
    duration: float


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    """音频携带格式信息，协议层不需要了解采样率和模型细节。"""

    content: bytes
    media_type: str = "audio/wav"
