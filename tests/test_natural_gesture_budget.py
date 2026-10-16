"""真实控制器以实际回执兑现节奏；假时钟只推进时间，不替换调度或领域规则。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain import presentation as domain
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision


def decision(gesture="none", style="gentle"):
    """只让语义端口提供受控高分结果，执行事实必须仍由生产控制器及回执产生。"""
    return PresentationDecision(DeliveryIntent(style, gesture), 1, 1, 1, 0.5)


async def settled():
    """让真实任务完成有限的已就绪工作，不能用放宽冷却或真实长睡眠制造通过。"""
    for _ in range(15):
        await asyncio.sleep(0)


async def controller_at(monkeypatch):
    """时钟仅替换接收回执的单调时间；Jev端口、真实候选槽和TimerHandle照常运行。"""
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(
        "xiaoya.application.presentation.monotonic", lambda: clock.value, raising=False
    )
    port = AsyncMock(
        decide=AsyncMock(return_value=decision("nod")),
        decide_user=AsyncMock(return_value=UserPresentationDecision(decision())),
        aclose=AsyncMock(),
    )
    controller = PresentationController(port)
    emitted = []
    controller.attach(emitted.append)
    controller.complete_turn("聊聊吧")
    await settled()
    return controller, clock, emitted


async def reply(controller, segment, gesture="nod", style="gentle", *, reply_id="reply"):
    """真实播放入口保留回复身份，跨回复节奏与同回复去重必须分别验证。"""
    text = f"这是{segment}句。"
    controller._decider.decide.return_value = decision(gesture, style)
    controller.prepare_reply(text)
    await settled()
    return controller.speak(reply_id, segment, DeliveryIntent(), text)


async def test_one_reply_does_not_repeat_a_natural_gesture_on_each_sentence(monkeypatch):
    """明显手势服务于一段回复的沟通意图，不能冷却一到就随着每句重复点头。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        assert acknowledge(controller, first, "started")
        assert acknowledge(controller, first, "completed")
        controller.finish("reply", "one")
        clock.value = 110.0
        second = await reply(controller, "two")
        assert second.intent == DeliveryIntent("gentle")
        assert second.gesture_id == "" and controller._natural_pending is None
        controller.finish("reply", "two")
        third = await reply(controller, "three", "tilt", "curious")
        assert third.intent.gesture == "tilt"
    finally:
        await controller.aclose()


async def test_unconfirmed_offer_dedup_does_not_claim_execution_or_block_new_reply(monkeypatch):
    """已安排但未确认的动作不补演，也不记完成；新回复仍可在原未知隔离后尝试。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        controller.finish("reply", "one")
        clock.value = 110.0
        second = await reply(controller, "two")
        assert second.gesture_id == "" and not controller._natural_starts
        assert controller._last_command == "none"
        controller.finish("reply", "two")
        next_reply = await reply(controller, "one", reply_id="new-reply")
        assert next_reply.intent.gesture == "nod"
        assert next_reply.gesture_id != first.gesture_id
    finally:
        await controller.aclose()


def acknowledge(controller, cue, status):
    """四字段协议的应用入口不额外相信浏览器时间或动作来源声明。"""
    return controller.acknowledge(cue.turn_id, cue.gesture_id, status)


@pytest.mark.parametrize(
    ("gesture", "starts", "now", "expected"),
    [
        ("nod", (), 100.0, 100.0),
        ("tilt", (("nod", 100.0),), 102.999, 103.0),
        ("nod", (("nod", 100.0),), 103.0, 106.0),
        ("tilt", (("nod", 100.0), ("wave", 103.0)), 106.0, 110.0),
        ("tilt", (("nod", 100.0), ("wave", 103.0)), 110.0, 110.0),
    ],
)
def test_original_natural_ready_at_has_exact_boundaries(gesture, starts, now, expected):
    """3秒、同类6秒、10秒两次全部保留，恰好边界可执行且不引入浮点容差。"""
    assert domain.natural_gesture_ready_at(gesture, starts, now) == expected


async def test_actual_start_not_offer_time_controls_first_cooldown(monkeypatch):
    """准备两秒后才真正发力时，下一动作不得借最初下发时间提早兑现3秒预算。"""
    controller, clock, emitted = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        assert first.intent.gesture == "nod"
        clock.value = 102.0
        assert acknowledge(controller, first, "started")
        assert controller._cue.gesture_id == first.gesture_id
        assert acknowledge(controller, first, "started")
        clock.value = 104.0
        assert acknowledge(controller, first, "completed")
        controller.finish("reply", "one")
        second = await reply(controller, "two", "tilt", "curious")
        assert second.intent == DeliveryIntent("curious")
        assert second.gesture_id == "" and controller._natural_timer is not None
        pending = controller._natural_pending
        clock.value = 104.999
        controller._resume_natural(pending)
        assert controller._cue.gesture_id == ""
        clock.value = 105.0
        controller._resume_natural(controller._natural_pending)
        assert emitted[-1].gesture_id == "reply:two:tilt"
        assert len(controller._natural_starts) == 1
        assert controller._last_command == "none"
    finally:
        await controller.aclose()


async def test_same_kind_and_two_in_ten_remain_independent(monkeypatch):
    """同类跨回复仍须6秒，不同类需3秒且第三次需10秒；去重不能代替原节奏门。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one", "nod")
        assert acknowledge(controller, first, "started")
        assert acknowledge(controller, first, "completed")
        controller.finish("reply", "one")
        clock.value = 103.0
        same = await reply(controller, "same", "nod", reply_id="second-reply")
        assert same.gesture_id == ""
        assert controller._natural_pending.intent.gesture == "nod"
        assert controller._natural_timer.when() - asyncio.get_running_loop().time() > 2.9
        controller.finish("second-reply", "same")
        second = await reply(controller, "two", "wave")
        assert second.intent.gesture == "wave"
        assert acknowledge(controller, second, "started")
        assert acknowledge(controller, second, "completed")
        controller.finish("reply", "two")
        clock.value = 106.0
        third = await reply(controller, "three", "tilt")
        assert third.gesture_id == ""
        clock.value = 109.999
        controller._resume_natural(controller._natural_pending)
        assert controller._cue.gesture_id == ""
        clock.value = 110.0
        controller._resume_natural(controller._natural_pending)
        assert controller._cue.gesture_id == "reply:three:tilt"
    finally:
        await controller.aclose()


async def test_finished_segment_never_borrows_next_audio_or_replays_timer(monkeypatch):
    """当前句结束即撤候选；已排入回调队列的旧timer也不能借下一句的身份补演。"""
    controller, clock, emitted = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        acknowledge(controller, first, "started")
        acknowledge(controller, first, "completed")
        controller.finish("reply", "one")
        clock.value = 101.0
        await reply(controller, "two", "tilt")
        old_pending = controller._natural_pending
        old_timer = controller._natural_timer
        controller.finish("reply", "two")
        assert old_timer.cancelled()
        assert controller._natural_pending is None and controller._natural_timer is None
        await reply(controller, "three", "wave")
        new_pending = controller._natural_pending
        clock.value = 110.0
        controller._resume_natural(old_pending)
        assert controller._natural_pending is new_pending
        assert not any(cue.gesture_id == "reply:two:tilt" for cue in emitted)
        controller._resume_natural(new_pending)
        assert controller._cue.gesture_id == "reply:three:wave"
    finally:
        await controller.aclose()


async def test_new_turn_keeps_started_budget_and_cancellation_does_not_refund(monkeypatch):
    """用户打断撤许可但不退还已开始预算，下一轮不能用新turn绕过原3秒限制。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        acknowledge(controller, first, "started")
        clock.value = 101.0
        acknowledge(controller, first, "canceled")
        controller.phase("listening")
        controller.complete_turn("新的问题")
        await settled()
        assert not acknowledge(controller, first, "started")
        second = await reply(controller, "two", "wave")
        assert second.gesture_id == ""
        clock.value = 103.0
        controller._resume_natural(controller._natural_pending)
        assert controller._cue.gesture_id == "reply:two:wave"
        assert len(controller._natural_starts) == 1
    finally:
        await controller.aclose()


async def test_lost_start_receipt_recovers_only_new_candidate_after_full_unknown_window(
    monkeypatch,
):
    """丢ACK不能按未执行放行；撤权后完整10秒保守恢复，只允许新的当前候选而不重演旧ID。"""
    controller, clock, emitted = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        clock.value = 101.0
        controller.phase("listening")
        controller.complete_turn("新问题")
        await settled()
        await reply(controller, "two", "wave")
        assert controller._cue.gesture_id == ""
        assert controller._natural_unknown_until == 111.0
        assert not acknowledge(controller, first, "started")
        clock.value = 110.999
        controller._resume_natural(controller._natural_pending)
        assert controller._cue.gesture_id == ""
        clock.value = 111.0
        controller._resume_natural(controller._natural_pending)
        assert controller._cue.gesture_id == "reply:two:wave"
        assert not controller._natural_starts
        assert sum(cue.gesture_id == first.gesture_id for cue in emitted) <= 1
    finally:
        await controller.aclose()


@pytest.mark.parametrize("terminal", ["canceled", "unavailable"])
async def test_terminal_without_start_does_not_prove_no_execution(monkeypatch, terminal):
    """RPC丢失或乱序时缺started的终态不是未执行证明，保留未知隔离且不污染命令完成记录。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        clock.value = 101.0
        assert acknowledge(controller, first, terminal)
        assert acknowledge(controller, first, terminal)
        assert controller._natural_unknown_until == 111.0
        assert controller._last_command == "none" and controller._receipts == {}
        controller.finish("reply", "one")
        second = await reply(controller, "two", "wave")
        assert second.gesture_id == ""
    finally:
        await controller.aclose()


async def test_old_segment_late_ack_only_accounts_its_catalogued_identity(monkeypatch):
    """同轮旧句迟started只补保守时间账，终态只释放自身，不能取消或完成新的实际动作。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        controller.finish("reply", "one")
        clock.value = 110.0
        second = await reply(controller, "two", "wave")
        assert second.intent.gesture == "wave"
        clock.value = 111.0
        assert acknowledge(controller, first, "started")
        assert acknowledge(controller, first, "canceled")
        assert controller._cue.gesture_id == second.gesture_id
        assert controller._natural_active == (second.turn_id, second.gesture_id)
        assert not controller.acknowledge(first.turn_id, "not-offered", "started")
        assert controller._last_command == "none"
    finally:
        await controller.aclose()


async def test_same_identity_is_offered_once_even_after_completed(monkeypatch):
    """同句样式更新不能让已经完成的动作重新出现，也不因幂等回执重复消耗预算。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        assert acknowledge(controller, first, "started")
        assert acknowledge(controller, first, "completed")
        clock.value = 120.0
        again = controller.speak("reply", "one", DeliveryIntent(), "这是one句。")
        assert again.gesture_id == "" and again.intent.style == "gentle"
        assert not acknowledge(controller, first, "started")
        assert len(controller._natural_starts) == 1
    finally:
        await controller.aclose()


@pytest.mark.parametrize("terminal", ["completed", "canceled", "unavailable"])
async def test_terminal_identity_rejects_late_start_without_touching_new_pending(
    monkeypatch, terminal
):
    """终态先封存；迟started不能新增预算、重排新候选timer或改变任何当前身份。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        if terminal == "completed":
            assert acknowledge(controller, first, "started")
        assert acknowledge(controller, first, terminal)
        controller.finish("reply", "one")
        clock.value = 101.0
        await reply(controller, "two", "tilt")
        pending, timer = controller._natural_pending, controller._natural_timer
        starts = tuple(controller._natural_starts)
        clock.value = 102.0
        assert not acknowledge(controller, first, "started")
        assert tuple(controller._natural_starts) == starts
        assert controller._natural_pending is pending and controller._natural_timer is timer
    finally:
        await controller.aclose()


async def test_completion_without_started_is_rejected_not_assumed_as_execution(monkeypatch):
    """完成必须有已确认开始，非法completed既不能释放执行槽也不能冒充自然动作事实。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        active = controller._natural_active
        assert not acknowledge(controller, first, "completed")
        assert controller._natural_active == active
        assert controller._natural_unknown_until == 0.0 and not controller._natural_starts
        clock.value = 101.0
        controller.finish("reply", "one")
        assert controller._natural_unknown_until == 111.0
    finally:
        await controller.aclose()


async def test_command_is_independent_of_natural_unknown_and_keeps_original_deadlines(monkeypatch):
    """保守未知只降级自动手势，明确指令及既有1.5/4秒闭环不能等待自然ACK或改写历史。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        await reply(controller, "one")
        clock.value = 101.0
        controller.phase("listening")
        controller._decider.decide_user.return_value = UserPresentationDecision(
            decision(), command="shake", command_confidence=1, command_probability=1
        )
        controller.complete_turn("摇摇头")
        await settled()
        command = controller._cue
        assert command.gesture_source == "command"
        assert controller._natural_unknown_until == 111.0
        assert controller._command_watch.get_name() == "xiaoya-command-deadline"
        assert acknowledge(controller, command, "started")
        assert acknowledge(controller, command, "completed")
        assert controller._last_command == "shake"
        assert not controller._natural_starts
    finally:
        await controller.aclose()


async def test_listening_revocation_preserves_expression_and_cancels_pending(monkeypatch):
    """VAD停声与final立即撤倾听动作，仍保持Jev表情；不让600ms以外的冷却timer复活旧许可。"""
    controller, clock, _ = await controller_at(monkeypatch)
    try:
        first = await reply(controller, "one")
        acknowledge(controller, first, "started")
        acknowledge(controller, first, "completed")
        clock.value = 101.0
        controller.user_speaking(True)
        controller._adopt_user(UserPresentationDecision(decision("tilt", "curious")))
        pending = controller._natural_pending
        assert pending is not None and controller._cue.intent == DeliveryIntent("curious")
        controller.user_speaking(False)
        assert controller._natural_pending is None and controller._natural_timer is None
        assert controller._cue.intent == DeliveryIntent("curious")
        clock.value = 120.0
        controller._resume_natural(pending)
        assert controller._cue.intent == DeliveryIntent("curious")
    finally:
        await controller.aclose()


async def test_close_releases_single_timer_and_bounded_ledger(monkeypatch):
    """关闭同步撤掉唯一timer及执行目录；即使回调已经就绪也不能复活动作或保留正文。"""
    controller, clock, _ = await controller_at(monkeypatch)
    first = await reply(controller, "one")
    acknowledge(controller, first, "started")
    acknowledge(controller, first, "completed")
    controller.finish("reply", "one")
    clock.value = 101.0
    await reply(controller, "two", "tilt")
    pending, timer = controller._natural_pending, controller._natural_timer
    await controller.aclose()
    assert timer.cancelled()
    assert controller._natural_pending is None and controller._natural_timer is None
    assert not controller._natural_offers
    clock.value = 120.0
    controller._resume_natural(pending)
    assert controller._cue.phase == "idle" and controller._cue.gesture_id == ""
    assert not acknowledge(controller, first, "started")


async def test_long_session_keeps_only_bounded_offers_and_recent_starts(monkeypatch):
    """长会话的不同回复不能累积无限身份；旧ID拒收且同回复去重不绕过容量检查。"""
    controller, clock, _ = await controller_at(monkeypatch)
    first = None
    try:
        for index in range(20):
            clock.value = 100.0 + index * 10.0
            cue = await reply(controller, f"part-{index}", "nod", reply_id=f"reply-{index}")
            first = first or cue
            assert cue.intent.gesture == "nod"
            assert acknowledge(controller, cue, "started")
            assert acknowledge(controller, cue, "completed")
            controller.finish(f"reply-{index}", f"part-{index}")
        assert len(controller._natural_offers) == 16
        assert len(controller._natural_starts) == 1
        assert not acknowledge(controller, first, "started")
        assert controller._natural_pending is None and controller._natural_timer is None
    finally:
        await controller.aclose()
