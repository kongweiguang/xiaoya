"""当前单条动作与ACK完成历史分开，防止旧点头被并入新的歪头请求。"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import llm

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision
from xiaoya.infrastructure.expressive_agent import ExpressiveAgent


def command_decision(command, confidence=1.0, probability=1.0):
    """只注入Jev端口的受控证据，不让用户文本或LLM答句授权实际动作。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent("gentle"), 1, 1, 1, 0.5),
        command=command,
        command_confidence=confidence,
        command_probability=probability,
    )


def make_port():
    """每次用户判断可独立挂起，便于验证回复上下文不会等待Jev或偷用历史目标。"""
    return AsyncMock(
        decide_user=AsyncMock(return_value=command_decision("nod")),
        decide=AsyncMock(return_value=None),
        aclose=AsyncMock(),
    )


async def completed_nod(controller):
    """历史记录必须从真实应用回执路径产生，不能预设last_command绕过ACK业务不变量。"""
    controller.complete_turn("点点头")
    await controller._user_task
    cue = controller._cue
    assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
    assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
    return cue


def test_web_compound_scope_is_only_current_user_message():
    """约束当前消息范围而不强制固定台词；提示回归不冒称真实模型已经服从。"""
    facts = AssistantEnvironment(channel="web").stable_instructions()
    assert "只有当前一条用户消息自身明确要求多个动作时，才询问先做哪个" in facts
    assert "不能把上一轮动作或最近完成记录与当前请求拼成复合指令" in facts
    assert "历史动作只在当前明确要求再做一次时作为引用" in facts
    assert "只有 completed 回执才能说做完" in facts


async def test_previous_completed_nod_does_not_become_current_tilt_or_compound():
    """复现真实连续点头再歪头：pending没有已知目标，当前目标只在本轮Jev采纳后出现。"""
    entered, release = asyncio.Event(), asyncio.Event()
    port = make_port()
    controller = PresentationController(port)
    try:
        previous = await completed_nod(controller)

        async def held_tilt(context):
            """等待外部判断的整个窗口都只能确认接收，旧动作不能填充当前目标。"""
            assert context.user_text == "歪歪头"
            entered.set()
            await release.wait()
            return command_decision("tilt")

        port.decide_user.side_effect = held_tilt
        controller.complete_turn("歪歪头")
        await entered.wait()
        pending = controller.command_context()
        assert "本次动作状态：pending。" in pending
        assert "本轮仍在执行或待执行的动作目标：尚未确认。" in pending
        assert "仅供当前明确再做一次引用的历史记录：" in pending
        assert "完成的动作：nod。" in pending
        assert "历史动作不属于本次请求，不能与当前动作合并" in pending
        assert not controller.acknowledge(previous.turn_id, previous.gesture_id, "completed")
        release.set()
        await controller._user_task
        facts = controller.command_context()
        assert "本次动作状态：pendingCommand。" in facts
        assert "本轮仍在执行或待执行的动作目标：tilt。" in facts
        assert "本轮仍在执行或待执行的动作目标：nod。" not in facts
        assert "不添加上轮动作，也不虚构需要二选一" in facts
        assert controller._command[1] == "tilt"
        assert port.decide_user.await_count == 2
    finally:
        release.set()
        await controller.aclose()


async def test_current_target_follows_live_tuple_not_completed_record():
    """开始与完成事实沿原ACK推进；命令撤销后不从历史重新创造当前可执行目标。"""
    port = make_port()
    controller = PresentationController(port)
    try:
        await completed_nod(controller)
        port.decide_user.return_value = command_decision("tilt")
        controller.complete_turn("歪歪头")
        await controller._user_task
        cue = controller._cue
        assert "本轮仍在执行或待执行的动作目标：tilt。" in controller.command_context()
        assert "完成的动作：nod。" in controller.command_context()
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        controller.phase("idle")
        started = controller.command_context()
        assert "本次动作状态：started。" in started
        assert "本轮仍在执行或待执行的动作目标：tilt。" in started
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        completed = controller.command_context()
        assert "本次动作状态：completed。" in completed
        assert "完成的动作：tilt。" in completed
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in completed
        assert "无待执行目标不否认completed事实" in completed
        assert controller._command is None
    finally:
        await controller.aclose()


@pytest.mark.parametrize("terminal", ["unknown", "unavailable", "canceled"])
async def test_failed_or_canceled_current_target_cannot_overwrite_completed_history(terminal):
    """下发和失败均不等于完成，历史ACK只供repeat引用，不能伪造当前成功或当前目标。"""
    port = make_port()
    controller = PresentationController(port)
    try:
        await completed_nod(controller)
        port.decide_user.return_value = command_decision("tilt")
        controller.complete_turn("歪歪头")
        await controller._user_task
        cue = controller._cue
        if terminal == "unknown":
            await controller._expire_command(0, cue.gesture_id)
        else:
            assert controller.acknowledge(cue.turn_id, cue.gesture_id, terminal)
        facts = controller.command_context()
        assert f"本次动作状态：{terminal}。" in facts
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in facts
        assert "完成的动作：nod。" in facts
        assert controller._last_command == "nod"
        assert not controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
    finally:
        await controller.aclose()


@pytest.mark.parametrize("confidence,probability", [(0.84, 1.0), (1.0, 0.89)])
async def test_current_words_never_supply_target_when_jev_threshold_rejects(
    confidence, probability
):
    """直接请求和历史都不能代替既有概率门；新上下文只描述应用实际采纳的命令。"""
    port = make_port()
    controller = PresentationController(port)
    try:
        await completed_nod(controller)
        port.decide_user.return_value = command_decision("tilt", confidence, probability)
        controller.complete_turn("歪歪头")
        await controller._user_task
        assert controller._command is None
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in controller.command_context()
        assert "本轮仍在执行或待执行的动作目标：tilt。" not in controller.command_context()
        assert controller._last_command == "nod"
    finally:
        await controller.aclose()


async def test_repeat_still_resolves_only_ack_completed_history_with_new_identity():
    """缩窄历史用途不能破坏明确repeat；领域解析和新执行身份继续由现有流程决定。"""
    port = make_port()
    controller = PresentationController(port)
    try:
        old = await completed_nod(controller)
        port.decide_user.return_value = command_decision("repeat")
        controller.complete_turn("再做一次")
        assert "本轮仍在执行或待执行的动作目标：尚未确认。" in controller.command_context()
        await controller._user_task
        current = controller._cue
        assert current.gesture_id != old.gesture_id and current.turn_id != old.turn_id
        assert "本轮仍在执行或待执行的动作目标：nod。" in controller.command_context()
        assert "本次动作状态：pendingCommand。" in controller.command_context()
    finally:
        await controller.aclose()


async def test_missing_repeat_record_and_failed_decision_keep_target_unconfirmed():
    """空历史需澄清，判断失败需重试；两条原闭环都不能从原文猜出当前已确认动作。"""
    port = make_port()
    port.decide_user.return_value = command_decision("repeat")
    controller = PresentationController(port)
    try:
        controller.complete_turn("再做一次")
        await controller._user_task
        assert "本次动作状态：clarify。" in controller.command_context()
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in controller.command_context()
        assert controller._last_command == "none"
        port.decide_user.return_value = None
        controller.complete_turn("歪歪头")
        await controller._user_task
        assert "本次动作状态：unavailable。" in controller.command_context()
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in controller.command_context()
        assert controller._cue.command_feedback == "retry"
    finally:
        await controller.aclose()


@pytest.mark.parametrize(
    "identity_kind,gesture",
    [("old-turn", "tilt"), ("current", "none"), ("current", "untrusted-target-content")],
)
async def test_prompt_target_rejects_stale_tuple_and_non_action_enums(identity_kind, gesture):
    """即使内部记录损坏也不能把旧轮或任意字符串提升为当前system事实，合法流程另有端到端回归。"""
    controller = PresentationController(make_port())
    try:
        controller.complete_turn("歪歪头")
        await controller._user_task
        prefix = controller.turn_id if identity_kind == "current" else "old-turn"
        controller._command = (f"{prefix}:command:fixture", gesture, 0.5, "pending")
        facts = controller.command_context()
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in facts
        assert "untrusted-target-content" not in facts
        assert "old-turn" not in facts
    finally:
        await controller.aclose()


async def test_generation_context_keeps_history_and_current_scope_separate_without_waiting():
    """实际SDK输入保留旧对话但尾部只取当前事实，不用固定模型回复冒充真实UX已经恢复。"""
    port = make_port()
    controller = PresentationController(port)
    agent = ExpressiveAgent(
        instructions="自然短答",
        tools=[],
        room=Mock(),
        voices={},
        clock=Mock(now=Mock(return_value=datetime(2026, 10, 15, tzinfo=UTC))),
        environment=AssistantEnvironment(channel="web"),
    )
    agent._presentation = controller
    context = llm.ChatContext()
    context.add_message(role="user", content="点点头")
    context.add_message(role="assistant", content="收到。")
    context.add_message(role="user", content="歪歪头")
    history = list(context.items)
    try:
        await completed_nod(controller)
        port.decide_user.return_value = command_decision("tilt")
        controller.complete_turn("歪歪头")
        pending = agent.generation_context(context)
        assert "本次动作状态：pending。" in pending.items[-1].text_content
        assert "本轮仍在执行或待执行的动作目标：尚未确认。" in pending.items[-1].text_content
        await controller._user_task
        adopted = agent.generation_context(context)
        assert "本轮仍在执行或待执行的动作目标：tilt。" in adopted.items[-1].text_content
        assert "仅供当前明确再做一次引用的历史记录" in adopted.items[-1].text_content
        assert "只有当前一条用户消息自身明确要求多个动作" in adopted.items[0].text_content
        assert context.items == history
        assert all(left is right for left, right in zip(context.items, history, strict=True))
    finally:
        await controller.aclose()
