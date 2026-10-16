"""动作回复只据当前执行事实，历史告别和其他失败分支不能冒充本轮状态。"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import llm

from xiaoya.application.presentation import PresentationController
from xiaoya.domain import assistant
from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision
from xiaoya.infrastructure.expressive_agent import ExpressiveAgent


def response_policy(state):
    """把缺失领域策略作为明确合同红灯，而不是在收集阶段阻断其余真实竞态测试。"""
    policy = getattr(assistant, "command_response_instructions", None)
    assert callable(policy), "缺少按当前受控状态选择答句事实的领域规则"
    return policy(state)


def decision(command="shake"):
    """仅从Jev端口注入已过原阈值的有限意图，测试正文不控制实际动作。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent(), 1, 1, 1, 0.5),
        command=command,
        command_confidence=1,
        command_probability=1,
    )


def make_port():
    """每个测试拥有自己的有限端口，不连接网络或依靠定时重试推进状态。"""
    return AsyncMock(
        decide_user=AsyncMock(return_value=decision()),
        decide=AsyncMock(return_value=None),
        aclose=AsyncMock(),
    )


def assert_not_failure_branch(facts):
    """原错误源是未发生分支也列为可说内容；正常快照不得携带两个失败标签的答词。"""
    assert "unavailable" not in facts
    assert "unknown" not in facts
    assert "如实说暂不可用" not in facts
    assert "unknown说明结果未确认" not in facts


@pytest.mark.parametrize(
    "state,required",
    [
        ("none", "没有已确认需执行的动作"),
        ("pending", "判断仍在进行"),
        ("pendingCommand", "尚未收到开始事实"),
        ("started", "已有开始事实"),
        ("completed", "已收到完成回执"),
        ("canceled", "已取消"),
        ("unavailable", "暂不可用"),
        ("unknown", "结果尚未确认"),
        ("clarify", "仍需澄清"),
    ],
)
def test_domain_policy_uses_only_the_actual_state(state, required):
    """逐状态保留成功、待判、取消及未知的真假边界，不用降低诚实要求掩盖实错。"""
    facts = response_policy(state)
    assert required in facts
    assert "固定答句" not in facts
    if state not in {"unavailable", "unknown"}:
        assert_not_failure_branch(facts)
    if state in {"pending", "pendingCommand"}:
        assert "不自述正在或已做" in facts
        assert "第一人称动作描述" in facts
    if state == "unknown":
        assert "不能自动重做" in facts
        assert "不能推断已经成功、明确失败或能力缺失" in facts
    if state == "completed":
        assert "没有待执行目标不否认完成事实" in facts


@pytest.mark.parametrize("state", ["pending-command", "failed", "", None, [], True])
def test_domain_policy_explicitly_rejects_invalid_state(state):
    """未知值显式拒绝，避免内部状态或任意文本被默认解释成成功或故障。"""
    with pytest.raises(ValueError, match="动作执行状态"):
        response_policy(state)


async def test_pending_generation_after_farewell_neither_waits_nor_predicts_unavailable():
    """复现真实先告别后摇头：慢Jev期间公开生成输入只描述待判断，不提供假失败分支。"""
    entered, release = asyncio.Event(), asyncio.Event()
    port = make_port()

    async def held(context):
        """显式事件保证生成发生在端口未返回时，无需猜测调度或睡眠时间。"""
        assert context.user_text == "摇摇头"
        entered.set()
        await release.wait()
        return decision()

    port.decide_user.side_effect = held
    controller = PresentationController(port)
    agent = ExpressiveAgent(
        instructions="自然短答",
        tools=[],
        room=Mock(),
        voices={},
        presentation=controller,
        environment=AssistantEnvironment(channel="web"),
        clock=Mock(now=Mock(return_value=datetime(2026, 10, 15, tzinfo=UTC))),
    )
    history = llm.ChatContext()
    history.add_message(role="user", content="下次再聊，再见。")
    history.add_message(role="assistant", content="下次见。")
    current = history.add_message(role="user", content="摇摇头")
    original = list(history.items)
    try:
        controller.complete_turn("摇摇头")
        await entered.wait()
        generated = agent.generation_context(history)
        tail = generated.items[-1].text_content
        assert "本次动作状态：pending。" in tail
        assert "本轮仍在执行或待执行的动作目标：尚未确认。" in tail
        assert_not_failure_branch(tail)
        assert "判断仍在进行" in tail
        assert "动作请求未started只说收到" in tail
        assert "只回应最后一条用户消息所表达的当下请求" in tail
        assert "不能因为上一轮已经告别" in tail
        assert "追加再见或结束对话" in tail
        assert generated.items[-2] is current
        assert history.items == original
        assert all(a is b for a, b in zip(history.items, original, strict=True))
        assert not controller._user_task.done()
        assert port.decide_user.await_count == 1
        assert controller._command is None
    finally:
        release.set()
        await controller.aclose()


async def test_current_command_context_follows_real_ack_without_unrelated_failure_text():
    """动作确实started/completed时只更新事实提示，不能重建动作或动摇最近完成记录。"""
    controller = PresentationController(make_port())
    try:
        controller.complete_turn("摇摇头")
        await controller._user_task
        cue = controller._cue
        active = controller._command
        before = controller.command_context()
        assert "本次动作状态：pendingCommand。" in before
        assert "本轮仍在执行或待执行的动作目标：shake。" in before
        assert "尚未收到开始事实" in before
        assert_not_failure_branch(before)
        assert controller._command is active
        assert controller._last_command == "none"
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        started = controller.command_context()
        assert "本次动作状态：started。" in started
        assert "已有开始事实" in started
        assert_not_failure_branch(started)
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        completed = controller.command_context()
        assert "本次动作状态：completed。" in completed
        assert "已收到完成回执" in completed
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in completed
        assert "完成的动作：shake。" in completed
        assert_not_failure_branch(completed)
        assert controller._command is None
        assert controller._last_command == "shake"
        assert controller._decider.decide_user.await_count == 1
    finally:
        await controller.aclose()


@pytest.mark.parametrize("status", ["unavailable", "unknown", "canceled"])
async def test_real_failure_unknown_or_cancel_keeps_only_its_truth(status):
    """缩窄正常答句不能丢掉真实失败或未知；取消也不能冒充完成或被替换为不可用。"""
    controller = PresentationController(make_port())
    try:
        controller.complete_turn("摇摇头")
        await controller._user_task
        cue = controller._cue
        if status == "unknown":
            await controller._expire_command(0, cue.gesture_id)
        else:
            assert controller.acknowledge(cue.turn_id, cue.gesture_id, status)
        facts = controller.command_context()
        assert f"本次动作状态：{status}。" in facts
        assert response_policy(status) in facts
        assert controller._last_command == "none"
        assert controller._command is None
        assert not controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        if status == "unavailable":
            assert "暂不可用" in facts and "unknown" not in facts
        elif status == "unknown":
            assert "结果尚未确认" in facts and "unavailable" not in facts
        else:
            assert_not_failure_branch(facts)
    finally:
        await controller.aclose()


async def test_retry_pending_does_not_reuse_previous_unavailable_or_completed_state():
    """上一轮明确不可用或成功不能污染新pending，历史完成记录仍只供明确repeat引用。"""
    port = make_port()
    controller = PresentationController(port)
    try:
        port.decide_user.return_value = None
        controller.complete_turn("摇摇头")
        await controller._user_task
        assert "本次动作状态：unavailable。" in controller.command_context()
        port.decide_user.return_value = decision("tilt")
        controller.complete_turn("歪歪头")
        pending = controller.command_context()
        assert "本次动作状态：pending。" in pending
        assert_not_failure_branch(pending)
        await controller._user_task
        cue = controller._cue
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        controller.complete_turn("再做一次")
        pending = controller.command_context()
        assert "本轮仍在执行或待执行的动作目标：尚未确认。" in pending
        assert "完成的动作：tilt。" in pending
        assert_not_failure_branch(pending)
    finally:
        await controller.aclose()


@pytest.mark.parametrize("gesture", ["none", "untrusted-target-content"])
async def test_prompt_state_guidance_keeps_target_enum_and_turn_authority(gesture):
    """新增领域文字规则不能把非法目标、旧轮动作或原文提升成高优先级事实。"""
    controller = PresentationController(make_port())
    try:
        controller.complete_turn("摇摇头")
        await controller._user_task
        controller._command = (f"{controller.turn_id}:command:fixture", gesture, 0.5, "pending")
        assert "本轮仍在执行或待执行的动作目标：无待执行目标。" in controller.command_context()
        assert "untrusted-target-content" not in controller.command_context()
        controller._command = ("old-turn:command:fixture", "wave", 0.5, "pending")
        assert "本轮仍在执行或待执行的动作目标：wave。" not in controller.command_context()
    finally:
        await controller.aclose()


def test_controller_rejects_invalid_execution_state_before_prompt_interpolation():
    """领域的受控状态拒绝必须接到实际应用入口，不把非法内部值插入system事实。"""
    controller = PresentationController()
    controller._execution_state = "untrusted-state-content"
    with pytest.raises(ValueError, match="动作执行状态"):
        controller.command_context()


def test_response_facts_reassert_latest_intent_after_history_for_every_channel():
    """动态尾部在旧历史之后重申沟通范围，保留原共情事实核对而不靠关键词控制动作。"""
    for channel in ["unknown", "web", "room", "console"]:
        facts = AssistantEnvironment(channel=channel).response_instructions()
        assert "只回应最后一条用户消息所表达的当下请求" in facts
        assert "不能因为上一轮已经告别" in facts
        assert "追加再见或结束对话" in facts
        assert "不能说你已经为它准备了" in facts
        assert "确实已知的经历可以如实回应" in facts
