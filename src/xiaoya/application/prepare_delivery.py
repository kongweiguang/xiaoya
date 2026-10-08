"""将固定业务文案与模型表达统一为可消费的句段。"""

from xiaoya.domain.delivery import DeliveryIntent, SpeechSegment


class PrepareDelivery:
    """显式开场白和流式回复共享规则，表现失败不改变对话正文。"""

    def prepare(self, text: str, style: str, gesture: str) -> SpeechSegment | None:
        """正文片段不等于可朗读内容；保留符号与空白，仅真正空输入不创建句段。"""
        if not text:
            return None
        return SpeechSegment(text=text, intent=DeliveryIntent.normalize(style, gesture))

    def greeting(self, text: str) -> SpeechSegment:
        """开场表达由程序确定，避免为固定问候增加模型延迟或随机动作。"""
        return SpeechSegment(text=text, intent=DeliveryIntent("happy", "wave"))
