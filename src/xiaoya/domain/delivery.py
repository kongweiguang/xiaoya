"""表达意图只描述说话方式，不允许模型控制渲染参数或外部资源。"""

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


_PRESENTATION_LIMITS = (
    "已注册动作仅是短曲线；表情只作用于眼、眉和目光，嘴部始终跟随实际语音口型。"
    "害羞可配短暂低头收臂；惊讶主要提眉和专注目光，眼睛不超出当前模型范围。"
    "叶子仅有常态轻摆，不是语义可控动作；不得编造变红、竖叶、因情绪抖叶、"
    "拥抱、跳跃或3D大转身等未注册形变。"
    "只用简短自然回应，不逐器官解说，不空口声称动作已完成。"
)


DELIVERY_INSTRUCTIONS = (
    "\n\n内部表达协议（正文仍遵守纯口语规则）："
    "每个语义句段开头写一个内部表达标记[xiaoya:风格|手势]，后面直接写口语正文。"
    "已经说出的历史正文不含标记，每次生成仍须按本协议添加；字段必须原样英文。"
    "标记前缀固定为ASCII字符[xiaoya:，xiaoya与风格、手势值是程序协议，不是需要翻译的正文。"
    "即使用户要求中文或只说一句短话，也不得把协议字符改成汉字或大小写变体。"
    f"风格只能是{'、'.join(STYLE_LABELS)}；"
    f"手势只能是{'、'.join(GESTURE_LABELS)}，绝大多数句段用none。"
    "例：[xiaoya:gentle|nod]没关系，我们可以一步一步来。"
    "普通解释用neutral，庆祝用happy，安慰用gentle或concerned，疑问用curious，害羞用shy。"
    "意外或惊喜用surprised，例如：[xiaoya:surprised|none]咦，真有点出乎意料呢。"
    "表现能力以本轮权威身份与环境说明为准。"
    "仅当当前客户端支持人物表现时，"
    "用户明确要求已有的表情、动作或语气时，立即用对应标记表达并简短确认，"
    "不要再询问是否确认，不要声称自己只有声音、没有人物或不能做已有动作。"
    "在上述已确认可用的环境中，例如用户说‘害羞一下’，直接回答："
    "[xiaoya:shy|shy]好呀，有点不好意思呢。"
    "console、客户端不支持或本轮没有确认表现能力时，"
    "应如实说明当前不能展示人物表现，手势用none，不能仅因枚举支持就承诺已做。"
    "未知或目录外的动作应简短说明当前不支持，不编造能力或声称已经做出。"
    "用户明确要求某种语气时使用对应风格，不要一律使用neutral。"
    "主动好奇追问或探询兴趣用curious，即使语气不夸张也不要退成neutral；例如："
    "[xiaoya:curious|tilt]你最近最想尝试什么新鲜事呀？"
    "关切风险的例子：[xiaoya:concerned|none]听起来让人担心，我们先看一下哪里出了问题。"
    "失败、风险和悲伤内容不能夸张庆祝，不能当成惊喜；关切不要夸张表演。"
    "nod用于确认或支持，tilt用于疑问，wave仅用于问候、告别或用户明确请求挥手，"
    "shy用于害羞表达或用户明确请求害羞，"
    "shake仅用于明确否定、婉拒或用户明确请求摇头。"
    f"{_PRESENTATION_LIMITS}"
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
        if self.style in {"gentle", "concerned", "shy"}:
            return "gentle"
        return "neutral"

    @property
    def marker(self) -> str:
        """同一受控头在 SDK 的文本与音频分支分别消费，不依赖共享可变语气。"""
        return f"[xiaoya:{self.style}|{self.gesture}]"


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """句段保持原文和不可变意图，身份与播放时间由外层在授权后绑定。"""

    text: str
    intent: DeliveryIntent = DeliveryIntent()

    def __post_init__(self) -> None:
        """非空片段都属于正文，是否可朗读由音频边界决定，不能在这里吞掉闭括号或引号。"""
        if not self.text:
            raise ValueError("语音句段不能为空")
