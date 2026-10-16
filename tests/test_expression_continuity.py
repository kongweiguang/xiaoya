"""验证同轮神态连续性与真实完成边界，动作和冻结声音不得借用这份缓存。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationDecision,
    UserPresentationDecision,
    choose_presentation,
)


def decision(style="concerned", confidence=0.95):
    """语义证据从端口提供，不靠回复关键词或降低分类门制造表情覆盖。"""
    return PresentationDecision(DeliveryIntent(style), confidence, 1, 0, 0.4)


def user(style="concerned", confidence=0.95, command="none"):
    """指令授权独立于神态，便于验证整段出声结束不误取消无声动作。"""
    return UserPresentationDecision(
        decision(style, confidence),
        command=command,
        command_confidence=1,
        command_probability=float(command != "none"),
    )


def port(reply=None, first_user=None):
    """每个测试独立持有端口，检查的是公开轮次及播放顺序而非供应商概率算法。"""
    return AsyncMock(
        decide_user=AsyncMock(return_value=first_user or user()),
        decide=AsyncMock(return_value=reply or decision(confidence=0.45)),
        aclose=AsyncMock(),
    )


async def until(predicate):
    """仅推进到已观察状态，不用墙钟睡眠掩盖异步顺序或无限等待。"""
    for _ in range(80):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("未出现预期的表现状态")


async def preview_ready(controller, text):
    """观察精确正文已判断后再开始播放，才能区分语义不足与尚未返回。"""
    controller.prepare_reply(text)
    await until(lambda: controller._accepted is not None and controller._accepted[0].text == text)


@pytest.mark.parametrize("phase", ["listening", "thinking", "speaking"])
def test_domain_low_confidence_preserves_only_current_expression(phase):
    """表情回退不能携带旧动作或改变已冻结的声音，达门的新中性仍有覆盖权。"""
    context = PresentationContext(
        phase,
        "turn",
        1,
        reply_id="r",
        segment_id="s",
        voice_style="happy",
        expression_style="concerned",
    )
    cue = choose_presentation(context, decision("happy", 0.45))
    assert cue.intent == DeliveryIntent("concerned")
    assert cue.gesture_id == "" and context.voice_style == "happy"
    assert choose_presentation(context, decision("neutral", 0.95)).intent.style == "neutral"


@pytest.mark.parametrize("invalid", ["unknown", True])
def test_expression_fallback_stays_in_controlled_catalog(invalid):
    """缓存属于领域值，非法供应商值不能绕过枚举验证进入渲染。"""
    with pytest.raises(ValueError):
        PresentationContext("thinking", "turn", 0, expression_style=invalid)


async def test_user_expression_continues_across_low_confidence_reply_segments():
    """安慰含义的两种温暖分类分票时，维持本轮已确认的关切；不替换语音事实。"""
    controller = PresentationController(port())
    try:
        controller.complete_turn("朋友搬走了，我很失落。")
        await until(lambda: controller._cue.intent.style == "concerned")
        await preview_ready(controller, "我在这儿。")
        first = controller.speak("reply", "first", DeliveryIntent(), "我在这儿。")
        assert first.intent == DeliveryIntent("concerned")
        assert controller.voice_for("我在这儿。").style == "neutral"
        controller.finish("reply", "first")
        await preview_ready(controller, "可以慢慢说。")
        second = controller.speak("reply", "second", DeliveryIntent(), "可以慢慢说。")
        assert second.intent == DeliveryIntent("concerned")
        controller.finish("reply", "second")
        controller.complete_reply("reply")
        assert controller._cue.phase == "idle" and controller._cue.intent.style == "neutral"
    finally:
        await controller.aclose()


async def test_high_confidence_neutral_resets_expression_before_next_uncertain_segment():
    """连续性不锁死悲伤，高分中性结论应改变本轮缓存，下一句低分延续新神态。"""
    decider = port(decision("neutral", 0.95))
    controller = PresentationController(decider)
    try:
        controller.complete_turn("请帮我梳理一下。")
        await until(lambda: controller._cue.intent.style == "concerned")
        await preview_ready(controller, "先整理事实。")
        assert (
            controller.speak("r", "s", DeliveryIntent(), "先整理事实。").intent.style == "neutral"
        )
        controller.finish("r", "s")
        decider.decide.return_value = decision("concerned", 0.45)
        await preview_ready(controller, "再考虑下一步。")
        assert (
            controller.speak("r", "next", DeliveryIntent(), "再考虑下一步。").intent.style
            == "neutral"
        )
    finally:
        await controller.aclose()


async def test_future_preview_cannot_change_current_expression():
    """未来句即使已判开心，也只能缓存到自己的正文；当前安慰不被预生成抢走。"""
    decider = port(decision())
    controller = PresentationController(decider)
    try:
        controller.complete_turn("我有点失落。")
        await until(lambda: controller._cue.intent.style == "concerned")
        await preview_ready(controller, "我理解。")
        controller.speak("r", "first", DeliveryIntent(), "我理解。")
        decider.decide.return_value = decision("happy", 0.95)
        await preview_ready(controller, "后来成功了。")
        assert controller._cue.intent.style == "concerned"
        next_cue = controller.speak("r", "second", DeliveryIntent(), "后来成功了。")
        assert next_cue.intent.style == "happy"
    finally:
        await controller.aclose()


async def test_new_input_revokes_previous_expression_before_its_own_judgment():
    """新输入立即失效旧神态，新轮低分不能以连续性名义复用上一轮情绪。"""
    decider = port()
    controller = PresentationController(decider)
    try:
        controller.complete_turn("我很难过。")
        await until(lambda: controller._cue.intent.style == "concerned")
        decider.decide_user.return_value = user("concerned", 0.45)
        controller.complete_turn("讲一下数字。")
        assert controller._cue.intent.style == "neutral"
        await until(lambda: controller._user_task.done())
        assert controller._cue.intent.style == "neutral"
    finally:
        await controller.aclose()


async def test_late_user_expression_cannot_revive_finished_reply():
    """回复正常结束后，用户判断迟到也不能重启已撤销的神态。"""
    release = asyncio.Event()

    async def delayed_user(context):
        """保留仍在途的用户判断，重现短回答先结束、分类后到达的实际顺序。"""
        await release.wait()
        return user("happy", 0.95)

    decider = port(decision())
    decider.decide_user.side_effect = delayed_user
    controller = PresentationController(decider)
    try:
        controller.complete_turn("短回答即可。")
        await preview_ready(controller, "我理解。")
        controller.speak("r", "s", DeliveryIntent(), "我理解。")
        controller.finish("r", "s")
        controller.complete_reply("r")
        release.set()
        await until(lambda: controller._user_task.done())
        assert controller._cue.phase == "idle" and controller._cue.intent == DeliveryIntent()
        assert controller._last_expression is None
    finally:
        release.set()
        await controller.aclose()


async def test_whole_reply_completion_preserves_command_ack_and_repeat():
    """整段神态结束与指令执行不同生命周期，声音先结束也须等待真实 completed 回执。"""
    decider = port(first_user=user("happy", command="wave"))
    controller = PresentationController(decider)
    try:
        controller.complete_turn("挥挥手。")
        await until(lambda: controller._cue.gesture_source == "command")
        command = controller._cue
        assert controller.acknowledge(command.turn_id, command.gesture_id, "started")
        controller.speak("r", "s", DeliveryIntent(), "好呀。")
        controller.finish("r", "s")
        controller.complete_reply("r")
        assert controller._cue.gesture_id == command.gesture_id
        assert controller.acknowledge(command.turn_id, command.gesture_id, "completed")
        decider.decide_user.return_value = user(command="repeat")
        controller.complete_turn("再做一次。")
        await until(lambda: controller._cue.gesture_source == "command")
        assert controller._cue.intent.gesture == "wave"
        assert controller._cue.gesture_id != command.gesture_id
    finally:
        await controller.aclose()


async def test_old_whole_reply_completion_cannot_seal_new_playback():
    """旧 handle 的 done 迟到时，不能封住同轮已经获得独立回复身份的新播放。"""
    controller = PresentationController(port())
    try:
        controller.complete_turn("请接着说。")
        await until(lambda: controller._cue.intent.style == "concerned")
        controller.speak("old", "first", DeliveryIntent(), "第一段。")
        controller.finish("old", "first")
        controller.speak("new", "second", DeliveryIntent(), "第二段。")
        controller.complete_reply("old")
        assert controller._cue.reply_id == "new"
        assert controller._cue.intent.style == "concerned"
        controller.finish("new", "second")
        controller.complete_reply("new")
        assert controller._cue.intent.style == "neutral"
    finally:
        await controller.aclose()


async def test_finished_playback_identity_cannot_reopen_expression():
    """正常完成也封存该回复，旧开流不能借冻结声音重启表情或播放许可。"""
    controller = PresentationController(port())
    try:
        controller.complete_turn("这一轮说完了。")
        await until(lambda: controller._cue.intent.style == "concerned")
        controller.speak("r", "s", DeliveryIntent(), "我理解。")
        controller.finish("r", "s")
        controller.complete_reply("r")
        closed = controller._cue
        late = controller.speak("r", "late", DeliveryIntent("happy"), "迟到正文。")
        assert late == closed and late.phase == "idle"
        assert late.intent.style == "neutral"
    finally:
        await controller.aclose()


async def test_old_completion_preserves_unbound_new_reply_preview():
    """新 handle 只有精确正文预判时，旧 handle 的完成不能丢掉新句判断或提前授权动作。"""
    decider = port(decision("happy", 0.95))
    controller = PresentationController(decider)
    try:
        controller.complete_turn("请继续。")
        await until(lambda: controller._cue.intent.style == "concerned")
        controller.speak("old", "first", DeliveryIntent(), "原来的回复。")
        controller.finish("old", "first")
        await preview_ready(controller, "新回复成功了。")
        controller.complete_reply("old")
        assert controller._cue.intent == DeliveryIntent()
        fresh = controller.speak("new", "second", DeliveryIntent(), "新回复成功了。")
        assert fresh.intent.style == "happy" and fresh.reply_id == "new"
    finally:
        await controller.aclose()


async def test_whole_completion_before_idle_event_closes_current_expression():
    """SDK 的 done 与 idle 顺序可变，完成边界不能从冻结声音保留旧神态。"""
    controller = PresentationController(port())
    try:
        controller.complete_turn("这一轮说完了。")
        await until(lambda: controller._cue.intent.style == "concerned")
        controller.speak("r", "s", DeliveryIntent("happy"), "当前回复。")
        controller.complete_reply("r")
        assert controller._cue.phase == "idle"
        assert controller._cue.intent == DeliveryIntent()
    finally:
        await controller.aclose()
