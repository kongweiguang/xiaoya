"""以真实600ms调度和可迟到的端口验证VAD、ASR与完整轮次的独立许可。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision


def listening_decision(command: str = "none") -> UserPresentationDecision:
    """故意提供足额语义证据，缺动作只能来自生命周期门，不能靠降低置信度掩盖失败。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent("curious", "tilt"), 0.99, 0.99, 0.99, 0.5),
        command=command,
        command_confidence=0.99,
        command_probability=0.99,
    )


def listening_port(command: str = "none") -> AsyncMock:
    """只替换外部语义端口，实际应用控制器、稳定等待和任务回收均原样执行。"""
    return AsyncMock(
        decide_user=AsyncMock(return_value=listening_decision(command)),
        decide=AsyncMock(return_value=None),
        aclose=AsyncMock(),
    )


async def test_asr_final_preserves_expression_and_never_authorizes_command_early():
    """直接final立即只读预取并保持中性；完成的查询也须等完整轮次提交才可确认命令。"""
    port = listening_port("wave")
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        turn = controller.turn_id
        controller.transcribe("挥挥手", final=True)
        task = controller._user_task
        assert task is not None
        await task
        assert port.decide_user.await_count == 1
        assert controller._cue.intent == DeliveryIntent()
        assert controller._cue.gesture_id == "" and controller._command is None
        assert not controller._user_final and controller._transcript_final
        controller.complete_turn("挥挥手")
        await controller._user_task
        assert controller._user_task is task and port.decide_user.await_count == 1
        assert controller.turn_id == turn
        assert controller._cue.gesture_source == "command"
        assert controller._cue.intent.gesture == "wave"
        assert not controller._user_speaking
    finally:
        await controller.aclose()


@pytest.mark.parametrize("boundary", ["asr-final", "asr-final-empty", "vad-stop"])
async def test_final_and_vad_stop_revoke_active_listening_but_keep_jev_expression(boundary):
    """撤权保留已有神态并移除主动作；非空final可只读预取，但不得提前采纳其新证据。"""
    port = listening_port()
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        controller.transcribe("我还在说今天的事情", final=False)
        await controller._user_task
        assert controller._cue.intent == DeliveryIntent("curious", "tilt")
        turn = controller.turn_id
        if boundary == "asr-final":
            controller.transcribe("我还在说今天的事情", final=True)
        elif boundary == "asr-final-empty":
            controller.transcribe("", final=True)
        else:
            controller.user_speaking(False)
        assert controller.turn_id == turn
        assert controller._cue.intent == DeliveryIntent("curious", "none")
        assert controller._cue.gesture_source == "none" and controller._cue.gesture_id == ""
        late = controller._current_cue(controller._context, listening_decision().presentation)
        assert late.intent == DeliveryIntent("curious", "none")
        if boundary == "asr-final":
            assert controller._user_task is not None
            await controller._user_task
            assert port.decide_user.await_count == 2
            assert controller._cue.intent == DeliveryIntent("curious", "none")
        else:
            assert controller._user_task is None and port.decide_user.await_count == 1
    finally:
        await controller.aclose()


@pytest.mark.parametrize("boundary", ["asr-final", "vad-stop"])
async def test_inflight_partial_cannot_revive_after_current_permission_is_revoked(boundary):
    """供应商故意忽略取消并成功返回，仍须由应用代次和当前许可阻止旧partial复活。"""
    started, release = asyncio.Event(), asyncio.Event()
    port = listening_port()

    async def late(context):
        """忽略一次取消模拟难以中断的外部请求，不能把取消成功作为安全前提。"""
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return listening_decision()

    port.decide_user.side_effect = late
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        controller.transcribe("仍在继续的句子", final=False)
        await asyncio.wait_for(started.wait(), 1)
        old = controller._user_task
        if boundary == "asr-final":
            controller.transcribe("仍在继续的句子", final=True)
        else:
            controller.user_speaking(False)
        await asyncio.sleep(0)
        release.set()
        await old
        assert controller._cue.gesture_id == "" and controller._command is None
        assert controller._cue.intent.gesture == "none"
    finally:
        release.set()
        await controller.aclose()
    assert not controller._owned_tasks


async def test_resumed_partial_waits_fresh_600ms_in_same_turn_and_rejects_old_generation():
    """final只读预取不授权；恢复同文partial重新稳定600ms，旧结果不能借新许可发力。"""
    started, release = asyncio.Event(), asyncio.Event()
    entered = []
    port = listening_port()

    async def branch(context):
        """记录真实事件循环时刻，第一次请求跨越final与恢复边界后才返回。"""
        entered.append(asyncio.get_running_loop().time())
        if len(entered) == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        return listening_decision()

    port.decide_user.side_effect = branch
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        controller.transcribe("相同的稳定前缀", final=False)
        await asyncio.wait_for(started.wait(), 1)
        old = controller._user_task
        turn = controller.turn_id
        controller.transcribe("相同的稳定前缀", final=True)
        await controller._user_task
        assert len(entered) == 2 and controller._cue.intent.gesture == "none"
        resumed_at = asyncio.get_running_loop().time()
        controller.transcribe("相同的稳定前缀", final=False)
        current = controller._user_task
        assert current is not old and controller.turn_id == turn
        release.set()
        await old
        assert controller._cue.intent.gesture == "none"
        await asyncio.sleep(0.3)
        assert len(entered) == 2 and controller._cue.gesture_id == ""
        await asyncio.wait_for(current, 1)
        assert len(entered) == 3 and entered[2] - resumed_at >= 0.6
        assert controller._cue.intent == DeliveryIntent("curious", "tilt")
        assert controller.turn_id == turn and controller._command is None
    finally:
        release.set()
        await controller.aclose()


async def test_listening_stability_rechecks_deadline_after_early_timer_wakeup(monkeypatch):
    """模拟Windows提前唤醒而不放宽600ms；定时器返回本身不能成为语义查询许可。"""
    original_sleep = asyncio.sleep
    early = asyncio.Event()
    delays = []
    port = listening_port()
    controller = PresentationController(port)

    async def early_once(delay):
        """只让首个稳定等待提前返回，其余等待仍由真实事件循环计时。"""
        delays.append(delay)
        if len(delays) == 1:
            await original_sleep(0.01)
            early.set()
        else:
            await original_sleep(delay)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "sleep", early_once)
            controller.user_speaking(True)
            started = asyncio.get_running_loop().time()
            controller.transcribe("我的兴趣是画画，还想了解更多", final=False)
            await asyncio.wait_for(early.wait(), 1)
            assert port.decide_user.await_count == 0
            await asyncio.wait_for(controller._user_task, 1)
            assert asyncio.get_running_loop().time() - started >= 0.6
            assert len(delays) >= 2 and 0 < delays[1] < 0.6
            assert port.decide_user.await_count == 1
    finally:
        await controller.aclose()


async def test_partial_without_active_vad_never_borrows_future_speaking_permission():
    """没有VAD许可不启动查询；后到VAD也不补查，新的partial才重新等待固定稳定期。"""
    port = listening_port()
    controller = PresentationController(port)
    try:
        controller.phase("listening")
        turn = controller.turn_id
        controller.transcribe("网络晚到的增量", final=False)
        assert controller._user_task is None and port.decide_user.await_count == 0
        assert controller._cue.intent == DeliveryIntent()
        controller.user_speaking(True)
        assert controller.turn_id == turn and controller._user_task is None
        assert controller._cue.intent.gesture == "none"
        controller.transcribe("网络晚到的增量，继续说", final=False)
        await controller._user_task
        assert controller._cue.intent == DeliveryIntent("curious", "tilt")
    finally:
        await controller.aclose()


@pytest.mark.parametrize("boundary", ["new-turn", "close"])
async def test_new_turn_and_close_reclaim_old_listening_generation(boundary):
    """旧请求即使在新轮次或关闭后返回成功，也不能写回姿态或遗留拥有任务。"""
    started, release = asyncio.Event(), asyncio.Event()
    port = listening_port()

    async def late(context):
        """故意让取消后的外部收尾仍需等待，验证会话实际等待而不是只清当前槽。"""
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return listening_decision()

    port.decide_user.side_effect = late
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        controller.transcribe("旧轮次的内容", final=False)
        await asyncio.wait_for(started.wait(), 1)
        old = controller._user_task
        turn = controller.turn_id
        if boundary == "new-turn":
            controller.phase("thinking")
            controller.user_speaking(True)
            assert controller.turn_id != turn
            await asyncio.sleep(0)
            release.set()
            await old
            assert controller._cue.gesture_id == "" and controller._command is None
        else:
            closing = asyncio.create_task(controller.aclose())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await closing
            controller.user_speaking(True)
            controller.transcribe("关闭后的增量", final=False)
            assert controller._cue.phase == "idle" and controller._cue.gesture_id == ""
            assert not controller._user_speaking and not controller._transcript_final
    finally:
        release.set()
        await controller.aclose()
    assert not controller._owned_tasks
