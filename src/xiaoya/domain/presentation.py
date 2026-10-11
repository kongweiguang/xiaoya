"""表现决策只描述可展示的意图，语音风格和实际播放许可始终由原链路决定。"""

import math
from dataclasses import dataclass, replace
from typing import Literal

from xiaoya.domain.delivery import STYLES, DeliveryIntent

PresentationPhase = Literal["idle", "listening", "thinking", "speaking"]
PHASES = frozenset({"idle", "listening", "thinking", "speaking"})


@dataclass(frozen=True, slots=True)
class PresentationContext:
    """不可变输入把迟到结果绑定到精确轮次及片段，不依赖供应商或框架对象。"""

    phase: PresentationPhase
    turn_id: str
    revision: int
    text: str = ""
    user_text: str = ""
    reply_id: str = ""
    segment_id: str = ""
    voice_style: str = "neutral"
    reply_preview: bool = False

    def __post_init__(self) -> None:
        """阶段和语义修订属于业务身份，非法构造必须在进入异步调度前失败。"""
        if (
            self.phase not in PHASES
            or not self.turn_id
            or type(self.revision) is not int
            or self.revision < 0
            or self.voice_style not in STYLES
            or type(self.reply_preview) is not bool
        ):
            raise ValueError("表现上下文阶段、轮次或修订号非法")
        if self.phase == "speaking" and (not self.reply_id or not self.segment_id):
            raise ValueError("说话表现必须绑定实际回复与句段")


@dataclass(frozen=True, slots=True)
class PresentationDecision:
    """分别保存分类置信度与执行概率，避免把不同 API 原语当作同一个分数。"""

    intent: DeliveryIntent
    style_confidence: float
    gesture_confidence: float
    perform_probability: float
    energy: float

    def __post_init__(self) -> None:
        """外部概率必须是有限实数，布尔值和 NaN 不能悄悄进入动画参数。"""
        for value in (
            self.style_confidence,
            self.gesture_confidence,
            self.perform_probability,
            self.energy,
        ):
            if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("表现分数必须是 0～1 的有限实数")


@dataclass(frozen=True, slots=True)
class PresentationCue:
    """稳定动作身份与可变表情分离，同一动作不能因样式更新而重复执行。"""

    phase: PresentationPhase = "idle"
    turn_id: str = ""
    reply_id: str = ""
    segment_id: str = ""
    intent: DeliveryIntent = DeliveryIntent()
    gesture_id: str = ""
    energy: float = 0.5

    def __post_init__(self) -> None:
        """控制器不能输出无身份的动作或超范围强度，非说话阶段只允许微表情。"""
        if (
            self.phase not in PHASES
            or type(self.energy) not in {float, int}
            or not math.isfinite(self.energy)
            or not 0 <= self.energy <= 1
        ):
            raise ValueError("表现阶段或强度非法")
        if self.phase == "speaking" and not all((self.turn_id, self.reply_id, self.segment_id)):
            raise ValueError("说话表现缺少播放身份")
        if self.intent.gesture != "none" and (self.phase != "speaking" or not self.gesture_id):
            raise ValueError("手势缺少合法说话阶段或执行身份")


def baseline_cue(context: PresentationContext) -> PresentationCue:
    """没有有效 Jev 判断只呈现自然姿态；已固定声音仅提供初始表情，不产生动作。"""
    intent = (
        DeliveryIntent(context.voice_style) if context.phase == "speaking" else DeliveryIntent()
    )
    return PresentationCue(
        phase=context.phase,
        turn_id=context.turn_id,
        reply_id=context.reply_id,
        segment_id=context.segment_id,
        intent=intent,
    )


def choose_presentation(
    context: PresentationContext, decision: PresentationDecision
) -> PresentationCue:
    """Jev 独占语义表现；声音一旦开始合成不再改写，视觉判断只受置信度及播放身份约束。"""
    cue = baseline_cue(context)
    style = cue.intent.style
    candidate = decision.intent
    if decision.style_confidence >= 0.65:
        style = candidate.style
    gesture = cue.intent.gesture
    if (
        context.phase == "speaking"
        and decision.gesture_confidence >= 0.85
        and decision.perform_probability >= 0.90
    ):
        gesture = candidate.gesture
    return replace(
        cue,
        intent=DeliveryIntent(style, gesture),
        gesture_id=(
            f"{context.reply_id}:{context.segment_id}:{gesture}" if gesture != "none" else ""
        ),
        energy=decision.energy,
    )
