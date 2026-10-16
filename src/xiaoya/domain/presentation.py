"""表现决策只描述可展示的意图，语音风格和实际播放许可始终由原链路决定。"""

import math
from dataclasses import dataclass, replace
from typing import Literal

from xiaoya.domain.delivery import GESTURES, STYLES, DeliveryIntent

PresentationPhase = Literal["idle", "listening", "thinking", "speaking"]
PHASES = frozenset({"idle", "listening", "thinking", "speaking"})
GestureSource = Literal["none", "dialogue", "command"]
GESTURE_SOURCES = frozenset({"none", "dialogue", "command"})


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
    recent_user_text: str = ""
    recent_assistant_text: str = ""
    last_command: str = "none"
    expression_style: str | None = None

    def __post_init__(self) -> None:
        """阶段和语义修订属于业务身份，非法构造必须在进入异步调度前失败。"""
        if (
            self.phase not in PHASES
            or not self.turn_id
            or type(self.revision) is not int
            or self.revision < 0
            or self.voice_style not in STYLES
            or type(self.reply_preview) is not bool
            or self.last_command not in GESTURES
            or (self.expression_style is not None and self.expression_style not in STYLES)
            or any(
                not isinstance(value, str)
                for value in (
                    self.text,
                    self.user_text,
                    self.recent_user_text,
                    self.recent_assistant_text,
                )
            )
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
    gesture_source: GestureSource = "dialogue"

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
        if self.gesture_source not in GESTURE_SOURCES:
            raise ValueError("表现动作来源非法")
        if self.gesture_source == "none" and self.intent.gesture != "none":
            raise ValueError("无来源的表现不能携带动作")


@dataclass(frozen=True, slots=True)
class UserPresentationDecision:
    """用户命令与自然倾听保持独立，回复语义不能替用户授权执行或解除禁止。"""

    presentation: PresentationDecision
    command: str = "none"
    command_confidence: float = 0.0
    command_probability: float = 0.0
    prohibited: str = "none"
    prohibition_confidence: float = 0.0
    prohibition_probability: float = 0.0

    def __post_init__(self) -> None:
        """固定动作目录和有限概率构成许可边界，重复动作只引用确认完成的会话记录。"""
        if (
            not isinstance(self.presentation, PresentationDecision)
            or self.presentation.gesture_source == "command"
            or self.command not in GESTURES | {"repeat"}
            or self.prohibited not in GESTURES | {"all"}
        ):
            raise ValueError("用户表现命令或禁止动作非法")
        for value in (
            self.command_confidence,
            self.command_probability,
            self.prohibition_confidence,
            self.prohibition_probability,
        ):
            if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("用户表现分数必须是 0～1 的有限实数")


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
    gesture_source: GestureSource = "none"
    command_feedback: Literal["none", "retry", "clarify"] = "none"

    def __post_init__(self) -> None:
        """指令独立于语音许可，自然说话仍要求精确片段；无身份或跨来源动作必须拒绝。"""
        if (
            self.phase not in PHASES
            or type(self.energy) not in {float, int}
            or not math.isfinite(self.energy)
            or not 0 <= self.energy <= 1
            or self.gesture_source not in GESTURE_SOURCES
            or self.command_feedback not in {"none", "retry", "clarify"}
        ):
            raise ValueError("表现阶段或强度非法")
        if (
            self.phase == "speaking"
            and self.gesture_source != "command"
            and not all((self.turn_id, self.reply_id, self.segment_id))
        ):
            raise ValueError("说话表现缺少播放身份")
        if self.intent.gesture != "none":
            if not self.turn_id or not self.gesture_id or self.gesture_source == "none":
                raise ValueError("手势缺少执行身份或来源")
            if self.gesture_source == "dialogue" and (
                self.phase not in {"listening", "speaking"}
                or self.phase == "listening"
                and self.intent.gesture not in {"nod", "tilt"}
            ):
                raise ValueError("自然手势不符合倾听或实际说话阶段")
        elif self.gesture_id or self.gesture_source != "none":
            raise ValueError("无动作表现不得保留执行身份或来源")


def baseline_cue(context: PresentationContext) -> PresentationCue:
    """同轮已采纳神态优先于初始声音；待机撤销神态，任何回退均不继承动作或声音许可。"""
    style = "neutral"
    if context.phase != "idle" and context.expression_style is not None:
        style = context.expression_style
    elif context.phase == "speaking":
        style = context.voice_style
    intent = DeliveryIntent(style)
    return PresentationCue(
        phase=context.phase,
        turn_id=context.turn_id,
        reply_id=context.reply_id,
        segment_id=context.segment_id,
        intent=intent,
    )


def choose_user_prohibition(decision: UserPresentationDecision) -> str:
    """用户明确禁止先于所有动作；低置信度或低许可概率不得把自然否定误作停止命令。"""
    if decision.prohibition_confidence >= 0.85 and decision.prohibition_probability >= 0.90:
        return decision.prohibited
    return "none"


def choose_user_command(decision: UserPresentationDecision, last_command: str = "none") -> str:
    """重复只引用确认完成记录，用户禁止始终优先；回复内容不能授权或撤销用户命令。"""
    if last_command not in GESTURES:
        raise ValueError("最近完成指令动作非法")
    if decision.command_confidence < 0.85 or decision.command_probability < 0.90:
        return "none"
    command = last_command if decision.command == "repeat" else decision.command
    if choose_user_prohibition(decision) in {"all", command}:
        return "none"
    return command


def natural_gesture_ready_at(
    gesture: str, starts: tuple[tuple[str, float], ...], now: float
) -> float:
    """实际开始的单调回执时间兑现原3/6/10秒规则；下发与取消都不能退还已开始预算。"""
    if (
        gesture not in GESTURES - {"none"}
        or type(now) not in {int, float}
        or not math.isfinite(now)
        or any(
            name not in GESTURES - {"none"}
            or type(at) not in {int, float}
            or not math.isfinite(at)
            or at > now
            for name, at in starts
        )
        or any(left[1] > right[1] for left, right in zip(starts, starts[1:], strict=False))
    ):
        raise ValueError("自然动作节奏必须使用有序、有限的实际开始回执")
    ready = now
    if starts:
        ready = max(ready, starts[-1][1] + 3.0)
    same = [at for name, at in starts if name == gesture]
    if same:
        ready = max(ready, same[-1] + 6.0)
    if len(starts) >= 2:
        ready = max(ready, starts[-2][1] + 10.0)
    return ready


def choose_presentation(
    context: PresentationContext, decision: PresentationDecision
) -> PresentationCue:
    """低置信度仅延续同轮神态，高置信度中性仍覆盖；动作保留独立门槛及精确播放身份。"""
    cue = baseline_cue(context)
    style = cue.intent.style
    candidate = decision.intent
    if decision.style_confidence >= 0.65:
        style = candidate.style
    gesture = cue.intent.gesture
    if (
        context.phase in {"listening", "speaking"}
        and not context.reply_preview
        and decision.gesture_source == "dialogue"
        and decision.gesture_confidence >= 0.85
        and decision.perform_probability >= 0.90
        and (context.phase != "listening" or candidate.gesture in {"nod", "tilt"})
    ):
        gesture = candidate.gesture
    return replace(
        cue,
        intent=DeliveryIntent(style, gesture),
        gesture_id=(
            (
                f"{context.reply_id}:{context.segment_id}:{gesture}"
                if context.phase == "speaking"
                else f"{context.turn_id}:listen:{gesture}"
            )
            if gesture != "none"
            else ""
        ),
        energy=decision.energy,
        gesture_source="dialogue" if gesture != "none" else "none",
    )
