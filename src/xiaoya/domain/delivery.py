"""Jev 的有限意图与纯正文分离，不允许模型控制渲染参数或外部资源。"""

from dataclasses import dataclass
from types import MappingProxyType

STYLE_LABELS = MappingProxyType(
    {
        "neutral": "自然",
        "happy": "开心",
        "gentle": "温柔",
        "concerned": "关切",
        "curious": "好奇",
        "shy": "害羞",
        "surprised": "惊讶",
    }
)
GESTURE_LABELS = MappingProxyType(
    {
        "none": "无额外动作",
        "nod": "点头",
        "tilt": "歪头",
        "wave": "挥手",
        "shy": "害羞",
        "shake": "摇头",
    }
)
STYLES = frozenset(STYLE_LABELS)
GESTURES = frozenset(GESTURE_LABELS)


def delivery_capability_summary() -> str:
    """实际渠道由装配确认；中文能力目录与协议共用枚举，避免数量和标签独立漂移。"""
    styles = "、".join(STYLE_LABELS.values())
    gestures = "、".join(label for name, label in GESTURE_LABELS.items() if name != "none")
    return f"表达风格：{styles}；可表演动作：{gestures}。"


@dataclass(frozen=True, slots=True)
class DeliveryIntent:
    """固定枚举确保声音和人物从同一个意图派生，不承载任意动作命令。"""

    style: str = "neutral"
    gesture: str = "none"

    def __post_init__(self) -> None:
        """内部值对象严格拒绝非法值，外部输入应先经过明确的中性降级。"""
        if self.style not in STYLES or self.gesture not in GESTURES:
            raise ValueError("表达风格或手势不受支持")

    @classmethod
    def normalize(cls, style: str, gesture: str) -> "DeliveryIntent":
        """损坏的模型指令整体降为中性，避免保留其中一个字段后产生意外表演。"""
        if style not in STYLES or gesture not in GESTURES:
            return cls()
        return cls(style=style, gesture=gesture)

    @property
    def voice_preset(self) -> str:
        """语义风格比声音预设更丰富，有限映射保持音色稳定并减少模型调参。"""
        if self.style == "happy":
            return "happy"
        if self.style in {"gentle", "concerned", "shy"}:
            return "gentle"
        return "neutral"


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """正文没有表现指令；身份、语气和动作由各自的实际运行边界绑定。"""

    text: str

    def __post_init__(self) -> None:
        """非空片段都属于正文，是否可朗读由音频边界决定，不能在这里吞掉闭括号或引号。"""
        if not self.text:
            raise ValueError("语音句段不能为空")
