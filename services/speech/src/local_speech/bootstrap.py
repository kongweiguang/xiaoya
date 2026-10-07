"""装配根是应用端口与具体模型实现相遇的位置。"""

import os

from local_speech.application import SpeechService
from local_speech.infrastructure import LocalSpeechModels
from local_speech.streaming_asr import ParaformerRecognizer


def create_service() -> SpeechService:
    """进程共享权重但不共享识别缓存；模型目录缺失必须阻止健康探测成功。"""
    streaming = ParaformerRecognizer(os.environ["SPEECH_STT_PATH"])
    return SpeechService(LocalSpeechModels(streaming), streaming)
