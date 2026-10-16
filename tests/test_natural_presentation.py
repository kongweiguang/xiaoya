"""真实应用调度验证动作来源及回执，人工语义仅注入端口而不代替客户端执行。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision


def user_decision(command="none", prohibited="none"):
    """把命令证据与自然表现分开，测试不能靠回复正文授权用户指令。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent("curious", "tilt"), 1, 1, 1, 0.5),
        command=command,
        command_confidence=1,
        command_probability=float(command != "none"),
        prohibited=prohibited,
        prohibition_confidence=1,
        prohibition_probability=float(prohibited != "none"),
    )


def port(decision=None):
    """独立用户和回复端口捕获是否发生等待、重放或跨输入借用。"""
    return AsyncMock(
        decide_user=AsyncMock(return_value=decision or user_decision()),
        decide=AsyncMock(
            return_value=PresentationDecision(DeliveryIntent("happy", "nod"), 1, 1, 1, 0.6)
        ),
        aclose=AsyncMock(),
    )


async def adopted(controller, expected="command"):
    """事件循环推进只作有界保护，条件来自当前执行身份而不是任意长睡眠。"""
    for _ in range(30):
        await asyncio.sleep(0)
        if controller._cue.gesture_source == expected:
            return controller._cue
    raise AssertionError("表现判断没有到达预期来源")


async def test_command_starts_without_reply_audio_and_survives_phase_changes():
    """指令是用户授权的独立执行，思考结束和静音不能撤销仍在执行的身份。"""
    decider = port(user_decision("shake"))
    controller = PresentationController(decider)
    try:
        controller.complete_turn("摇摇头")
        cue = await adopted(controller)
        assert cue.phase == "thinking" and cue.intent.gesture == "shake"
        decider.decide.assert_not_awaited()
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        spoken = controller.speak("reply", "segment", DeliveryIntent(), "好呀。")
        assert spoken.gesture_id == cue.gesture_id
        finished = controller.finish("reply", "segment")
        assert finished.phase == "idle" and finished.gesture_id == cue.gesture_id
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        assert controller._cue.gesture_source == "none"
        assert "shake" in controller.command_context()
    finally:
        await controller.aclose()


async def test_completed_receipts_are_idempotent_and_repeat_has_new_identity():
    """重复请求必须是新执行，重复回执则不能重新生成动作或覆盖终态。"""
    decider = port(user_decision("wave"))
    controller = PresentationController(decider)
    try:
        controller.complete_turn("挥挥手")
        first = await adopted(controller)
        assert not controller.acknowledge(first.turn_id, first.gesture_id, "completed")
        assert controller.acknowledge(first.turn_id, first.gesture_id, "started")
        assert controller.acknowledge(first.turn_id, first.gesture_id, "completed")
        assert controller.acknowledge(first.turn_id, first.gesture_id, "completed")
        assert not controller.acknowledge(first.turn_id, first.gesture_id, "started")
        decider.decide_user.return_value = user_decision("repeat")
        controller.complete_turn("再做一次")
        second = await adopted(controller)
        assert second.intent.gesture == "wave" and second.gesture_id != first.gesture_id
    finally:
        await controller.aclose()


async def test_repeat_without_confirmed_completion_requires_clarification():
    """已经下发但没完成的动作不能成为再次执行的语义依据。"""
    controller = PresentationController(port(user_decision("repeat")))
    try:
        controller.complete_turn("再做一次")
        for _ in range(15):
            await asyncio.sleep(0)
        assert controller._cue.command_feedback == "clarify"
        assert controller._cue.gesture_source == "none"
    finally:
        await controller.aclose()


@pytest.mark.parametrize("prohibited", ["shake", "all"])
async def test_prohibition_precedes_command_and_natural_gesture(prohibited):
    """同轮禁令覆盖指令和自动手势，不能通过声音开始重新取得动画许可。"""
    controller = PresentationController(port(user_decision("shake", prohibited)))
    try:
        controller.complete_turn("不要摇头")
        for _ in range(15):
            await asyncio.sleep(0)
        assert controller._command is None
        controller.prepare_reply("好的，我会安静听你说。")
        for _ in range(15):
            await asyncio.sleep(0)
        cue = controller.speak("r", "s", DeliveryIntent(), "好的，我会安静听你说。")
        if prohibited == "all":
            assert cue.intent.gesture == "none"
        else:
            assert cue.intent.gesture != "shake"
    finally:
        await controller.aclose()


async def test_new_input_cancels_command_and_rejects_late_completion():
    """新用户开口是强取消边界，迟到完成不能更新最近动作或恢复旧姿态。"""
    controller = PresentationController(port(user_decision("nod")))
    try:
        controller.complete_turn("点点头")
        old = await adopted(controller)
        controller.acknowledge(old.turn_id, old.gesture_id, "started")
        controller.phase("listening")
        assert controller._cue.gesture_id == ""
        assert not controller.acknowledge(old.turn_id, old.gesture_id, "completed")
        assert controller._last_command == "none"
    finally:
        await controller.aclose()


async def test_command_timeout_is_unknown_and_does_not_replay():
    """丢失回执与真实失败不同，未知结果只能提示重新请求，不能自动产生重复动作。"""
    decider = port(user_decision("wave"))
    controller = PresentationController(decider)
    try:
        controller.complete_turn("挥挥手")
        cue = await adopted(controller)
        controller._command_watch.cancel()
        await controller._expire_command(0, cue.gesture_id)
        assert controller._cue.command_feedback == "retry"
        assert controller._cue.gesture_id == ""
        assert not controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        controller.prepare_reply("你好呀，很高兴见到你。")
        for _ in range(15):
            await asyncio.sleep(0)
        repeated = controller.speak("r", "s", DeliveryIntent(), "你好呀，很高兴见到你。")
        assert repeated.gesture_id == ""
        decider.decide_user.assert_awaited_once()
    finally:
        await controller.aclose()


async def test_reply_preview_does_not_cancel_independent_user_judgment():
    """首句语义可以提前判断，尚未返回的用户命令仍保留自己的所有权。"""
    release = asyncio.Event()
    decider = port()

    async def wait_user(context):
        """阻塞只发生在外部增强端口，测试能观察语音预判是否仍然继续。"""
        await release.wait()
        return user_decision("tilt")

    decider.decide_user.side_effect = wait_user
    controller = PresentationController(decider)
    try:
        controller.complete_turn("歪歪头")
        controller.prepare_reply("好呀。")
        for _ in range(15):
            await asyncio.sleep(0)
        decider.decide.assert_awaited_once()
        assert controller._user_task is not None and not controller._user_task.done()
        release.set()
        await adopted(controller)
    finally:
        await controller.aclose()


async def test_stable_listening_can_react_but_cannot_execute_command():
    """真实VAD活跃的稳定增量可轻回应，partial语义不能替代完整轮次的命令授权。"""
    decider = port(user_decision("wave"))
    controller = PresentationController(decider)
    try:
        controller.user_speaking(True)
        controller.transcribe("我还在讲昨天发生的事情", final=False)
        await asyncio.sleep(0.61)
        for _ in range(15):
            await asyncio.sleep(0)
        assert controller._cue.gesture_source == "dialogue"
        assert controller._cue.intent.gesture == "tilt" and controller._command is None
        controller.complete_turn("挥挥手")
        assert (await adopted(controller)).intent.gesture == "wave"
    finally:
        await controller.aclose()


async def test_two_preview_slots_preserve_first_and_next_playing_sentence():
    """快速生成不能用第三句抢走第一句，合成入口随后能为被省略预判的正文重新提交。"""
    controller = PresentationController(port())
    try:
        controller.complete_turn("聊聊吧")
        controller.prepare_reply("第一句。")
        controller.prepare_reply("第二句。")
        controller.prepare_reply("第三句。")
        assert controller._preview.text == "第一句。"
        assert controller._next_preview.text == "第二句。"
        for _ in range(15):
            await asyncio.sleep(0)
        first = controller.speak("r", "one", DeliveryIntent(), "第一句。")
        assert first.intent.gesture == "nod"
        assert controller._preview.text == "第二句。"
        controller.finish("r", "one")
        controller.prepare_reply("第三句。")
        assert controller._next_preview.text == "第三句。"
    finally:
        await controller.aclose()


async def test_close_supervises_replaced_user_tasks_until_cleanup_finishes():
    """旧请求被取消后仍属于会话，关闭不能只等待当前槽而遗漏旧请求的异步收尾。"""
    entered, cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    decider = port()

    async def held_user(context):
        """外部端口在取消时异步收尾，用真实竞争证明任务替换不会丢失拥有权。"""
        if context.user_text != "旧输入":
            return user_decision()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()

    decider.decide_user.side_effect = held_user
    controller = PresentationController(decider)
    controller.complete_turn("旧输入")
    await entered.wait()
    controller.complete_turn("新输入")
    await cleanup_started.wait()
    closing = asyncio.create_task(controller.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    release_cleanup.set()
    await closing
    assert not controller._owned_tasks


async def test_current_audio_segment_preempts_obsolete_slow_judgment():
    """短句不能等旧句预算耗尽；新实际播放身份取消过期判断，匹配预判晋升另有回归。"""
    old_entered, old_canceled, new_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    decider = port()

    async def delayed_reply(context):
        """用悬挂旧请求复现播放竞争，下一句必须立即开始判断而非靠超时解除队列。"""
        if context.text == "旧句。":
            old_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                old_canceled.set()
        new_entered.set()
        return PresentationDecision(DeliveryIntent("happy", "nod"), 1, 1, 1, 0.6)

    decider.decide.side_effect = delayed_reply
    controller = PresentationController(decider)
    try:
        controller.complete_turn("聊聊吧")
        controller.speak("r", "old", DeliveryIntent(), "旧句。")
        await old_entered.wait()
        controller.finish("r", "old")
        controller.speak("r", "new", DeliveryIntent(), "新句。")
        await asyncio.wait_for(new_entered.wait(), 0.3)
        assert old_canceled.is_set()
    finally:
        await controller.aclose()


async def test_recent_context_only_contains_normally_completed_previous_reply():
    """当前轮中间片段不冒充上一轮，打断和迟到done也不能提交新轮的未完成文本。"""
    controller = PresentationController(port())
    try:
        controller.complete_turn("第一轮问题")
        controller.speak("first", "one", DeliveryIntent(), "第一句。")
        controller.finish("first", "one")
        assert controller._recent_assistant == ""
        controller.speak("first", "two", DeliveryIntent(), "第二句。")
        controller.finish("first", "two")
        controller.complete_reply("first")
        assert controller._recent_assistant == "第一句。第二句。"
        controller.complete_turn("第二轮问题")
        controller.speak("second", "one", DeliveryIntent(), "被打断的内容。")
        controller.finish("second", "one")
        controller.complete_turn("新输入")
        controller.complete_reply("second")
        assert controller._recent_user == "第一轮问题"
        assert controller._recent_assistant == "第一句。第二句。"
    finally:
        await controller.aclose()


async def test_growing_transcript_preserves_stable_prefix_reaction():
    """VAD活跃的连续partial仍有600ms稳定前缀，追加词不重启；final边界另有独立回归。"""
    controller = PresentationController(port(user_decision("wave")))
    try:
        controller.user_speaking(True)
        controller.transcribe("我今天终于考过了", final=False)
        await asyncio.sleep(0.3)
        controller.transcribe("我今天终于考过了，真的很高兴", final=False)
        await asyncio.sleep(0.32)
        for _ in range(15):
            await asyncio.sleep(0)
        assert controller._cue.gesture_source == "dialogue"
        assert controller._command is None
        assert controller._decider.decide_user.await_args.args[0].user_text == "我今天终于考过了"
    finally:
        await controller.aclose()


async def test_repeat_dialogue_clarifies_only_when_completion_record_is_missing():
    """回复上下文不能无条件要求重复命令澄清，否则数字动作成功而口头却再次询问。"""
    controller = PresentationController(port(user_decision("shy")))
    try:
        controller.complete_turn("害羞一下")
        cue = await adopted(controller)
        controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        facts = controller.command_context()
        assert "完成的动作：shy" in facts and "不再追问" in facts
    finally:
        await controller.aclose()
