"""未发力的权威终态只释放自身未知账，不能按丢失 started 推断没有执行。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationContext, PresentationCue


def setup_controller(monkeypatch):
    """仅替换接收侧单调时钟，保留生产装饰器、目录、冷却和当前候选定时器。"""
    clock = SimpleNamespace(value=31.4261)
    monkeypatch.setattr("xiaoya.application.presentation.monotonic", lambda: clock.value)
    controller = PresentationController()
    controller._prohibited = "none"
    controller._user_speaking = True
    return controller, clock


def emit(controller, *, phase="listening", gesture="nod", segment="one", turn="turn"):
    """语义端口只注入明确高置信候选；是否公开仍经过真实当前身份和预算规则。"""
    reply = "reply" if phase == "speaking" else ""
    segment = segment if phase == "speaking" else ""
    identity = f"{turn}:listen:{gesture}" if phase == "listening" else f"reply:{segment}:{gesture}"
    controller._context = PresentationContext(phase, turn, 1, reply_id=reply, segment_id=segment)
    cue = PresentationCue(
        phase=phase,
        turn_id=turn,
        reply_id=reply,
        segment_id=segment,
        intent=DeliveryIntent("happy", gesture),
        gesture_id=identity if gesture != "none" else "",
        gesture_source="dialogue" if gesture != "none" else "none",
    )
    controller._emit(cue)
    return controller._cue


async def test_saved_mic17_ambiguous_cancel_reproduces_unknown_block(monkeypatch):
    """MIC17 的 31.4261/31.5444/31.554 秒事件没有发力证明，原十秒隔离确会遮掉首句候选。"""
    controller, clock = setup_controller(monkeypatch)
    try:
        listening = emit(controller)
        clock.value = 31.5444
        emit(controller, gesture="none")
        assert controller._natural_unknown_until == pytest.approx(41.5444)
        clock.value = 31.554
        assert controller.acknowledge(listening.turn_id, listening.gesture_id, "canceled")
        assert controller._natural_unknown_until == pytest.approx(41.554)
        assert not controller._natural_starts
        clock.value = 34.3317
        speaking = emit(controller, phase="speaking")
        assert speaking.intent == DeliveryIntent("happy") and speaking.gesture_id == ""
        assert controller._natural_pending is not None
    finally:
        await controller.aclose()


async def test_explicit_unstarted_cancel_releases_only_own_hold_and_current_pending(monkeypatch):
    """有渲染器 false 事实才能区分准备取消与丢失 started，仍不重演已终止的倾听身份。"""
    controller, clock = setup_controller(monkeypatch)
    try:
        listening = emit(controller)
        clock.value = 31.5444
        emit(controller, gesture="none")
        clock.value = 34.3317
        assert emit(controller, phase="speaking").gesture_id == ""
        assert controller.acknowledge(
            listening.turn_id, listening.gesture_id, "canceled", stroke_started=False
        )
        assert controller._natural_unknown_until == 0
        assert controller._cue.gesture_id == "reply:one:nod"
        assert not controller._natural_starts
        assert not controller.acknowledge(listening.turn_id, listening.gesture_id, "started")
        assert controller._cue.gesture_id == "reply:one:nod"
    finally:
        await controller.aclose()


@pytest.mark.parametrize("stroke_started", [None, True])
async def test_absent_or_true_stroke_fact_keeps_original_unknown(monkeypatch, stroke_started):
    """遗漏或已发力的终态不能退款，旧客户端和丢 started 都继续降级完整十秒。"""
    controller, clock = setup_controller(monkeypatch)
    try:
        first = emit(controller)
        clock.value = 32
        assert controller.acknowledge(
            first.turn_id, first.gesture_id, "canceled", stroke_started=stroke_started
        )
        assert controller._natural_unknown_until == 42
        assert emit(controller, phase="speaking").gesture_id == ""
    finally:
        await controller.aclose()


async def test_unstarted_fact_never_clears_another_unknown_or_started_budget(monkeypatch):
    """同轮旧身份可能迟到，释放自己的准备取消不得清掉另一动作的未知或真实开始账。"""
    controller, clock = setup_controller(monkeypatch)
    try:
        first = emit(controller)
        clock.value = 32
        emit(controller, gesture="none")
        clock.value = 42
        second = emit(controller, phase="speaking")
        assert second.gesture_id
        clock.value = 42.5
        assert controller.acknowledge(first.turn_id, first.gesture_id, "unavailable")
        assert controller._natural_unknown_until == 52.5
        clock.value = 43
        emit(controller, phase="thinking", gesture="none")
        assert controller._natural_unknown_until == 53
        assert controller.acknowledge(
            second.turn_id, second.gesture_id, "canceled", stroke_started=False
        )
        assert controller._natural_unknown_until == 52.5
        assert not controller._natural_starts
    finally:
        await controller.aclose()


async def test_received_started_cannot_be_refunded_by_false_terminal(monkeypatch):
    """后端真实收到 started 比矛盾 false 更强，取消仍保留同类六秒及不同类三秒预算。"""
    controller, clock = setup_controller(monkeypatch)
    try:
        first = emit(controller)
        assert controller.acknowledge(first.turn_id, first.gesture_id, "started", True)
        clock.value = 32
        assert controller.acknowledge(first.turn_id, first.gesture_id, "canceled", False)
        clock.value = 34.3317
        assert emit(controller, phase="speaking").gesture_id == ""
        assert controller._natural_starts == [("nod", 31.4261)]
        pending = controller._natural_pending
        clock.value = controller._natural_starts[0][1] + 6.0
        controller._resume_natural(pending)
        assert controller._cue.gesture_id == "reply:one:nod"
    finally:
        await controller.aclose()


@pytest.mark.parametrize("bad", [0, 1, "false", [], {}])
async def test_invalid_stroke_fact_cannot_mutate_ledger(monkeypatch, bad):
    """Python 的 bool/int 关系不能让非法 JSON 数值被当作未执行证明。"""
    controller, _ = setup_controller(monkeypatch)
    try:
        first = emit(controller)
        assert not controller.acknowledge(first.turn_id, first.gesture_id, "canceled", bad)
        assert not controller._natural_offers[(first.turn_id, first.gesture_id)].terminal
        assert not controller._natural_unknown_until
    finally:
        await controller.aclose()


async def test_old_turn_duplicate_terminal_and_unavailable_cannot_refund(monkeypatch):
    """终态只消费一次，旧轮或 unavailable 的 false 都不是当前候选的重新执行许可。"""
    controller, clock = setup_controller(monkeypatch)
    try:
        first = emit(controller)
        assert controller.acknowledge(first.turn_id, first.gesture_id, "canceled")
        assert controller.acknowledge(first.turn_id, first.gesture_id, "canceled", False)
        assert controller._natural_unknown_until == pytest.approx(41.4261)
        clock.value = 42
        second = emit(controller, phase="speaking")
        assert controller.acknowledge(second.turn_id, second.gesture_id, "unavailable", False)
        assert controller._natural_unknown_until == 52
        controller._context = replace(controller._context, turn_id="new-turn")
        assert not controller.acknowledge(first.turn_id, first.gesture_id, "canceled", False)
        assert controller._natural_unknown_until == 52
    finally:
        await controller.aclose()
