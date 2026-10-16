"""验证慢连接下的可见终态与执行事实，避免端口单测遗漏应用截止和生命周期竞争。"""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from xiaoya.application import presentation
from xiaoya.application.presentation import PresentationController
from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING, PresentationTiming


def decision(command="none"):
    """高置信度假证据只控制端口，不用回复文本授权动作或替代客户端回执。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent("gentle", "nod"), 1, 1, 1, 0.5),
        command=command,
        command_confidence=1,
        command_probability=float(command != "none"),
    )


def make_port():
    """预热和两类判断独立可控，防止测试通过共享完成条件掩盖语音等待。"""
    return AsyncMock(
        warmup=AsyncMock(),
        decide_user=AsyncMock(return_value=decision()),
        decide=AsyncMock(
            return_value=PresentationDecision(DeliveryIntent("happy", "nod"), 1, 1, 1, 0.5)
        ),
        aclose=AsyncMock(),
    )


async def flush():
    """仅排空已就绪任务，不用长睡眠假装等待网络恢复。"""
    for _ in range(15):
        await asyncio.sleep(0)


def test_timing_is_controlled_and_immutable():
    """产品预算不可由单个适配器构造时覆盖，否则双层截止又会发生静默分歧。"""
    assert PRESENTATION_TIMING == PresentationTiming()
    assert (
        PRESENTATION_TIMING.user_total,
        PRESENTATION_TIMING.reply_total,
        PRESENTATION_TIMING.listening_total,
        PRESENTATION_TIMING.application_grace,
    ) == (8.0, 3.5, 1.2, 0.2)
    with pytest.raises(FrozenInstanceError):
        PRESENTATION_TIMING.user_total = 1.6
    with pytest.raises(TypeError):
        PresentationTiming(user_total=1.6)


@pytest.mark.parametrize("failure", [None, TimeoutError(), RuntimeError()])
async def test_complete_user_failure_is_visible_and_keeps_actions_closed(failure):
    """未产生指令身份的故障也需要反馈，回复自然手势不能绕过未知用户禁令。"""
    port = make_port()
    if isinstance(failure, Exception):
        port.decide_user.side_effect = failure
    else:
        port.decide_user.return_value = None
    controller = PresentationController(port)
    try:
        controller.complete_turn("挥挥手")
        await controller._user_task
        assert controller._cue.command_feedback == "retry"
        assert controller._prohibited == "all" and controller._command is None
        assert "本次动作状态：unavailable" in controller.command_context()
        assert "完成的动作：none" in controller.command_context()
        controller.prepare_reply("收到。")
        await flush()
        cue = controller.speak("reply", "segment", DeliveryIntent(), "收到。")
        assert cue.intent.gesture == "none" and cue.command_feedback == "retry"
    finally:
        await controller.aclose()


async def test_bad_user_port_hits_guard_and_reports_retry(monkeypatch):
    """应用额外余量只保护不守约定的端口，真实超时仍必须取消等待并形成可见失败。"""
    real_timeout = asyncio.timeout
    budgets = []
    canceled = asyncio.Event()
    port = make_port()

    def short_guard(delay):
        """缩短真实截止而保留取消语义，测试不需要实际等待八秒。"""
        budgets.append(delay)
        return real_timeout(0.001)

    async def broken(context):
        """悬挂端口的 finally 可证明外层兜底确实撤销了当前等待者。"""
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(presentation.asyncio, "timeout", short_guard)
    port.decide_user.side_effect = broken
    controller = PresentationController(port)
    try:
        controller.complete_turn("点点头")
        await controller._user_task
        assert budgets == [pytest.approx(8.2)] and canceled.is_set()
        assert controller._cue.command_feedback == "retry"
        assert "本次动作状态：unavailable" in controller.command_context()
    finally:
        await controller.aclose()


async def test_application_guards_leave_margin_for_each_port_total(monkeypatch):
    """倾听明确提供仍在说话的partial前置；三类合法请求的原固定截止余量保持不变。"""
    real_timeout = asyncio.timeout
    budgets = []

    def observe_guard(delay):
        """观察现有截止且保留真实事件循环，不用 Mock 成功代替实际调度。"""
        budgets.append(delay)
        return real_timeout(delay)

    monkeypatch.setattr(presentation.asyncio, "timeout", observe_guard)
    controller = PresentationController(make_port())
    try:
        controller.complete_turn("聊聊吧")
        await controller._user_task
        controller.prepare_reply("你好呀。")
        await flush()
        controller.user_speaking(True)
        controller.transcribe("今天终于考过了", final=False)
        await controller._user_task
        assert budgets == pytest.approx([8.2, 3.7, 1.4])
    finally:
        await controller.aclose()


@pytest.mark.parametrize("late_outcome", [None, RuntimeError(), decision("wave")])
async def test_late_old_user_outcome_cannot_replace_new_command(late_outcome):
    """即使坏端口吞掉取消，旧失败、旧成功都不能污染新轮重试提示或动作许可。"""
    entered, release = asyncio.Event(), asyncio.Event()
    port = make_port()

    async def held_old(context):
        """旧输入故意不立即响应取消，用真实竞争验证应用自己的身份边界。"""
        if context.user_text != "旧输入":
            return decision("shake")
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        if isinstance(late_outcome, Exception):
            raise late_outcome
        return late_outcome

    port.decide_user.side_effect = held_old
    controller = PresentationController(port)
    try:
        controller.complete_turn("旧输入")
        await entered.wait()
        old = controller._user_task
        controller.complete_turn("摇摇头")
        await controller._user_task
        current = controller._command
        assert current is not None and current[1] == "shake"
        release.set()
        await old
        assert controller._command == current
        assert controller._cue.command_feedback == "none"
        assert controller._prohibited == "none"
        assert "本次动作状态：pendingCommand" in controller.command_context()
    finally:
        release.set()
        await controller.aclose()


async def test_final_input_upgrades_started_listening_and_rejects_old_same_text():
    """先以VAD活跃的合法partial启动倾听，再完整提交同全文；旧轻回应不能抢占指令。"""
    entered, release = asyncio.Event(), asyncio.Event()
    port = make_port()

    async def branch(context):
        """监听分支故意迟到且返回不同动作，证明修订身份不只比较文本。"""
        if context.phase != "listening":
            return decision("shake")
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return decision("wave")

    port.decide_user.side_effect = branch
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        controller.transcribe("摇摇头", final=False)
        await entered.wait()
        old = controller._user_task
        controller.complete_turn("摇摇头")
        await controller._user_task
        current = controller._command
        assert port.decide_user.await_count == 2
        assert current is not None and current[1] == "shake"
        release.set()
        await old
        assert controller._command == current
        assert controller._cue.command_feedback == "none"
    finally:
        release.set()
        await controller.aclose()


async def test_final_failure_revokes_prior_partial_permission():
    """合法VAD活跃partial的轻回应许可不代表最终无禁令；完整失败仍必须关闭动作。"""
    port = make_port()
    controller = PresentationController(port)
    try:
        controller.user_speaking(True)
        controller.transcribe("今天终于考过了", final=False)
        await controller._user_task
        assert controller._prohibited == "none"
        port.decide_user.return_value = None
        controller.complete_turn("今天终于考过了，不要点头")
        assert controller._prohibited == "all"
        await controller._user_task
        assert controller._prohibited == "all"
        assert controller._cue.command_feedback == "retry"
    finally:
        await controller.aclose()


async def test_prepare_is_nonblocking_idempotent_and_owned_until_close():
    """慢预热不延迟用户任务，同一会话只准备一次，挂断必须回收等待。"""
    entered, canceled = asyncio.Event(), asyncio.Event()
    port = make_port()

    async def warmup():
        """连接等待与语义端口独立，finally 暴露真实取消而不伪造关闭成功。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    port.warmup.side_effect = warmup
    controller = PresentationController(port)
    controller.prepare()
    first = controller._prepare_task
    controller.prepare()
    assert controller._prepare_task is first
    await entered.wait()
    controller.complete_turn("聊聊吧")
    await controller._user_task
    assert not first.done() and controller._execution_state == "none"
    await controller.aclose()
    controller.prepare()
    port.warmup.assert_awaited_once()
    port.aclose.assert_awaited_once()
    assert canceled.is_set() and first.cancelled()
    assert not controller._owned_tasks


async def test_prepare_failure_is_supervised_and_does_not_break_next_user(caplog):
    """免费准备故障只记类别，下一次显式输入仍可恢复，不伪装成语音启动失败。"""
    port = make_port()
    port.warmup.side_effect = RuntimeError("do-not-log-connection-secret")
    port.decide_user.return_value = decision("wave")
    controller = PresentationController(port)
    try:
        controller.prepare()
        await controller._prepare_task
        assert "RuntimeError" in caplog.text and "do-not-log" not in caplog.text
        controller.complete_turn("挥挥手")
        await controller._user_task
        assert controller._command is not None
        assert controller._cue.command_feedback == "none"
    finally:
        await controller.aclose()


async def test_command_facts_follow_current_receipts_and_preserve_completed_history():
    """待判断、已下发、已开始和已完成是不同事实，下一轮不可沿用上一轮完成状态。"""
    port = make_port()
    port.decide_user.return_value = decision("shake")
    controller = PresentationController(port)
    try:
        controller.complete_turn("摇摇头")
        assert "本次动作状态：pending。" in controller.command_context()
        assert "动作请求未started只说收到" in controller.command_context()
        await controller._user_task
        cue = controller._cue
        assert "本次动作状态：pendingCommand。" in controller.command_context()
        assert "完成的动作：none" in controller.command_context()
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        assert "本次动作状态：started。" in controller.command_context()
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        assert "本次动作状态：completed。" in controller.command_context()
        assert "完成的动作：shake" in controller.command_context()
        controller.complete_turn("聊聊别的吧")
        assert "本次动作状态：pending。" in controller.command_context()
        assert "完成的动作：shake" in controller.command_context()
        assert not controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
    finally:
        await controller.aclose()


async def test_missing_receipt_keeps_unknown_in_current_facts():
    """回执丢失不能被最近完成记录掩盖，更不能把当前状态回退为普通无指令。"""
    port = make_port()
    port.decide_user.return_value = decision("wave")
    controller = PresentationController(port)
    try:
        controller.complete_turn("挥挥手")
        await controller._user_task
        cue = controller._cue
        await controller._expire_command(0, cue.gesture_id)
        assert "本次动作状态：unknown。" in controller.command_context()
        assert "完成的动作：none" in controller.command_context()
        assert controller._cue.command_feedback == "retry"
    finally:
        await controller.aclose()


def test_missing_decider_reports_unavailable_without_creating_tasks():
    """未配置判断端口也是明确不可用，不能静默答应动作或要求先等回执超时。"""
    controller = PresentationController()
    controller.complete_turn("挥挥手")
    controller.prepare()
    assert controller._cue.command_feedback == "retry"
    assert controller._prohibited == "all" and controller._prepare_task is None
    assert "本次动作状态：unavailable。" in controller.command_context()


def test_web_prompt_requires_started_and_completed_facts():
    """提示需阻止对未执行动作的现在时承诺，同时保留明确指令和停止语义。"""
    facts = AssistantEnvironment(channel="web").instructions(datetime(2026, 10, 15, tzinfo=UTC))
    assert "未到 started 时只简短回应收到" in facts
    assert "不自述正在执行或已经完成" in facts
    assert "只有 completed 回执才能说做完" in facts
    assert "unavailable 时如实说明暂不可用" in facts
    assert "用户明确说不要、不做或取消" in facts
