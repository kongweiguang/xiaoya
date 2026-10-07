"""表达意图只描述说话方式，不允许模型控制渲染参数或外部资源。"""

from dataclasses import dataclass

STYLES = frozenset({"neutral", "happy", "gentle", "concerned", "curious"})
GESTURES = frozenset({"none", "nod", "tilt", "wave"})

DELIVERY_INSTRUCTIONS = (
    "\n\n内部表达协议（正文仍遵守纯口语规则）："
    "每个语义句段开头写一个内部表达标记[风格|手势]，后面直接写口语正文。"
    "风格只能是neutral、happy、gentle、concerned、curious；"
    "手势只能是none、nod、tilt、wave，绝大多数句段用none。"
    "例：[gentle|nod]没关系，我们可以一步一步来。"
    "普通解释用neutral，庆祝用happy，安慰用gentle或concerned，疑问用curious。"
    "用户明确要求某种语气时使用对应风格，不要一律使用neutral。"
    "主动好奇追问或探询兴趣用curious，即使语气不夸张也不要退成neutral；例如："
    "[curious|tilt]你最近最想尝试什么新鲜事呀？"
    "关切风险的例子：[concerned|none]听起来让人担心，我们先看一下哪里出了问题。"
    "失败、风险和悲伤内容不能庆祝；关切不要夸张表演。"
    "nod用于确认或支持，tilt用于疑问，wave仅用于问候、告别或用户明确请求挥手。"
    "同一回复可以随内容改变风格，不要连续重复手势。"
    "标记不是正文，不解释标记；工具调用保持原有工具协议，工具结果后再给口语回复。"
)


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
        if self.style in {"gentle", "concerned"}:
            return "gentle"
        return "neutral"

    @property
    def marker(self) -> str:
        """同一受控头在 SDK 的文本与音频分支分别消费，不依赖共享可变语气。"""
        return f"[{self.style}|{self.gesture}]"


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """句段保持原文和不可变意图，身份与播放时间由外层在授权后绑定。"""

    text: str
    intent: DeliveryIntent = DeliveryIntent()

    def __post_init__(self) -> None:
        """纯标点不能触发合成或手势，但正文中的标点和空格保持原样。"""
        if not any(character.isalnum() for character in self.text):
            raise ValueError("语音句段必须包含正文")
