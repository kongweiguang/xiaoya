"""将固定业务文案与模型表达统一为可消费的句段。"""

from xiaoya.domain.delivery import DeliveryIntent, SpeechSegment


class PrepareDelivery:
    """显式开场白和流式回复共享规则，表现失败不改变对话正文。"""

    def prepare(self, text: str, style: str, gesture: str) -> SpeechSegment | None:
        """独立符号不构成说话内容；非法意图降级而不丢弃已经生成的有效正文。"""
        if not any(character.isalnum() for character in text):
            return None
        return SpeechSegment(text=text, intent=DeliveryIntent.normalize(style, gesture))

    def greeting(self, text: str) -> SpeechSegment:
        """开场表达由程序确定，避免为固定问候增加模型延迟或随机动作。"""
        return SpeechSegment(text=text, intent=DeliveryIntent("happy", "wave"))
