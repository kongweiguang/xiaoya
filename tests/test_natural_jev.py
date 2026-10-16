"""领域门独立验证倾听、自然说话和用户授权，语义正确也不能绕过执行边界。"""

from dataclasses import replace

import pytest

from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationCue,
    PresentationDecision,
    UserPresentationDecision,
    choose_presentation,
    choose_user_command,
    choose_user_prohibition,
)


def high_decision(gesture: str = "nod") -> PresentationDecision:
    """使用高分证据隔离语义模型误差，验证确定性许可、身份和取消约束。"""
    return PresentationDecision(DeliveryIntent("gentle", gesture), 1, 1, 1, 0.4)


@pytest.mark.parametrize(
    "gesture,expected",
    [("nod", "nod"), ("tilt", "tilt"), ("wave", "none"), ("shy", "none"), ("shake", "none")],
)
def test_listening_permits_only_small_responses_with_stable_identity(
    gesture: str, expected: str
) -> None:
    """倾听只允许轻回应，转写修订不能换执行身份从而在同一轮连续点头。"""
    context = PresentationContext("listening", "turn", 1, user_text="我想学习做饭")
    a = choose_presentation(context, high_decision(gesture))
    b = choose_presentation(replace(context, revision=8), high_decision(gesture))
    assert a.intent.gesture == expected
    assert a.gesture_id == b.gesture_id
    assert a.gesture_source == ("dialogue" if expected != "none" else "none")
    if expected != "none":
        assert a.gesture_id == f"turn:listen:{gesture}"


@pytest.mark.parametrize("phase", ["idle", "thinking"])
def test_idle_and_thinking_have_expression_but_no_natural_gesture(phase: str) -> None:
    """未在倾听或实际说话时只恢复神态，避免把迟到预判当成一段已经开始的音频。"""
    cue = choose_presentation(PresentationContext(phase, "turn", 1), high_decision())
    assert cue.intent == DeliveryIntent("gentle")
    assert cue.gesture_id == "" and cue.gesture_source == "none"


def test_reply_preview_cannot_execute_even_when_context_says_speaking() -> None:
    """预判标志明确阻止抢播，调用者误标阶段也不能让下一句动作借用当前句许可。"""
    context = PresentationContext(
        "speaking", "turn", 1, reply_id="r", segment_id="s", reply_preview=True
    )
    cue = choose_presentation(context, high_decision("wave"))
    assert cue.intent == DeliveryIntent("gentle")
    assert cue.gesture_id == ""


@pytest.mark.parametrize("phase", ["idle", "listening", "thinking", "speaking"])
def test_command_cue_is_independent_of_voice_phase(phase: str) -> None:
    """已授权命令只需轮次与执行身份，不依赖声音、回复或嘴部播放许可。"""
    cue = PresentationCue(
        phase,
        "turn",
        intent=DeliveryIntent("neutral", "shake"),
        gesture_id="c",
        gesture_source="command",
    )
    assert cue.intent.gesture == "shake" and not cue.reply_id and not cue.segment_id


@pytest.mark.parametrize("source", ["command", "none"])
def test_natural_selector_does_not_execute_other_source(source: str) -> None:
    """自然选择器不能代替用户授权路径，即使高分命令也需要应用层单独构造执行许可。"""
    context = PresentationContext("speaking", "turn", 1, reply_id="r", segment_id="s")
    decision = high_decision("none" if source == "none" else "wave")
    cue = choose_presentation(context, replace(decision, gesture_source=source))
    assert cue.intent.gesture == "none" and cue.gesture_source == "none"


@pytest.mark.parametrize(
    "confidence,probability,expected",
    [(0.849, 1, "none"), (1, 0.899, "none"), (0.85, 0.9, "shake")],
)
def test_command_requires_both_original_gates(
    confidence: float, probability: float, expected: str
) -> None:
    """自然度不能靠放宽许可获得，分类置信度与动作概率继续分别守原阈值。"""
    decision = UserPresentationDecision(high_decision(), "shake", confidence, probability)
    assert choose_user_command(decision) == expected


@pytest.mark.parametrize(
    "confidence,probability,expected", [(0.849, 1, "none"), (1, 0.899, "none"), (0.85, 0.9, "all")]
)
def test_prohibition_requires_both_original_gates(
    confidence: float, probability: float, expected: str
) -> None:
    """低分普通否定不能误抑制本轮表现，明确禁止按同一证据门决定。"""
    decision = UserPresentationDecision(
        high_decision(),
        prohibited="all",
        prohibition_confidence=confidence,
        prohibition_probability=probability,
    )
    assert choose_user_prohibition(decision) == expected


@pytest.mark.parametrize(
    "last,prohibited,expected",
    [
        ("none", "none", "none"),
        ("wave", "none", "wave"),
        ("wave", "wave", "none"),
        ("wave", "all", "none"),
        ("wave", "shake", "wave"),
    ],
)
def test_repeat_uses_only_completed_command_and_respects_prohibition(
    last: str, prohibited: str, expected: str
) -> None:
    """调用者传入的历史必须来自完成回执，重复标签本身不能猜测动作或绕过当前禁止。"""
    decision = UserPresentationDecision(high_decision(), "repeat", 1, 1, prohibited, 1, 1)
    assert choose_user_command(decision, last) == expected


@pytest.mark.parametrize(
    "field",
    [
        "command_confidence",
        "command_probability",
        "prohibition_confidence",
        "prohibition_probability",
    ],
)
@pytest.mark.parametrize("value", [True, "0.9", -0.1, 1.1, float("nan"), float("inf")])
def test_user_decision_rejects_invalid_probabilities(field: str, value: object) -> None:
    """授权分数拒绝 Python 特殊值及 JSON 污染，非法值不能被默认转换成高置信度。"""
    with pytest.raises(ValueError):
        UserPresentationDecision(high_decision(), **{field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"command": "dance"},
        {"prohibited": "repeat"},
        {"presentation": None},
        {"presentation": PresentationDecision(DeliveryIntent(), 1, 1, 1, 0.5, "command")},
    ],
)
def test_user_decision_rejects_cross_source_and_unknown_commands(change: dict) -> None:
    """用户输入值对象不接收另一条执行路径的结果，动作枚举保持封闭。"""
    values = {"presentation": high_decision(), **change}
    with pytest.raises(ValueError):
        UserPresentationDecision(**values)


@pytest.mark.parametrize(
    "change",
    [
        {"turn_id": ""},
        {"gesture_id": ""},
        {"gesture_source": "none"},
        {"gesture_source": "unknown"},
    ],
)
def test_command_cue_rejects_missing_identity(change: dict) -> None:
    """协议必须能唯一识别和撤销执行，缺少身份或未知来源不能进入前端。"""
    values = {
        "phase": "thinking",
        "turn_id": "turn",
        "intent": DeliveryIntent("neutral", "nod"),
        "gesture_id": "c",
        "gesture_source": "command",
        **change,
    }
    with pytest.raises(ValueError):
        PresentationCue(**values)
