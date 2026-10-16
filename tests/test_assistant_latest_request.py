"""保留公开的连续未答历史，用当前停止请求取代旧动作待办而不解析文字授权动作。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import llm

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision
from xiaoya.infrastructure.expressive_agent import ExpressiveAgent
from xiaoya.infrastructure.livekit_conversation import (
    LiveKitVoiceConversation,
    TextInputEvent,
)


@pytest.mark.parametrize("channel", ["unknown", "web", "room", "console"])
def test_latest_stop_request_overrides_unanswered_action_without_rewriting_history(channel):
    """所有接入渠道共享最新请求优先级，不把固定台词或测试提示当成模型行为证据。"""
    facts = AssistantEnvironment(channel=channel).stable_instructions()
    assert "以最后一条用户消息为当前请求" in facts
    assert "停止、取消或禁止动作，就覆盖此前尚未答复的动作请求" in facts
    assert "不能先答应或补做旧动作再停止" in facts
    assert "不能把连续两条用户输入合并为按顺序执行的动作请求" in facts
    assert "失败或结果未确认仍须如实说明" in facts


@pytest.mark.parametrize("stop_decision", ["prohibited", "unavailable"])
async def test_public_text_wave_then_stop_keeps_two_user_messages_and_only_current_state(
    private_settings, stop_decision
):
    """真实公开入口先清SDK语音累计再答复，保留未答历史且不让旧挥手覆盖当前停止。"""
    calls, contexts = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    neutral = PresentationDecision(DeliveryIntent(), 1, 1, 1, 0.5)
    decisions = [
        UserPresentationDecision(
            neutral, command="wave", command_confidence=1, command_probability=1
        ),
        UserPresentationDecision(
            neutral,
            prohibited="all",
            prohibition_confidence=1,
            prohibition_probability=1,
        )
        if stop_decision == "prohibited"
        else None,
    ]

    async def decide_user(context):
        """按调用序号提供受控Jev证据，不用关键词或正文生成器充当动作语义分类器。"""
        result = decisions.pop(0)
        if not decisions:
            entered.set()
            await release.wait()
        return result

    port = AsyncMock(
        decide_user=AsyncMock(side_effect=decide_user),
        decide=AsyncMock(return_value=None),
        aclose=AsyncMock(),
    )
    controller = PresentationController(port)
    agent = ExpressiveAgent(
        instructions="自然简短回答",
        tools=[],
        room=Mock(),
        voices={},
        presentation=controller,
        environment=AssistantEnvironment(channel="web"),
        clock=Mock(now=Mock(return_value=datetime(2026, 10, 15, tzinfo=UTC))),
    )
    history = llm.ChatContext()

    async def interrupt():
        """仅记录SDK公开打断完成点，旧命令回收仍由真实应用新轮次实现。"""
        calls.append("interrupt")

    def clear_user_turn():
        """文字入口必须同步清SDK语音累计，夹具保留公开边界以验证打断后才开始新回复。"""
        calls.append("clear")

    def generate_reply(*, user_input):
        """模拟SDK把当前用户文本加入公开历史，不生成固定答句或调用外部LLM。"""
        calls.append(("reply", user_input))
        history.add_message(role="user", content=user_input)
        contexts.append(agent.generation_context(history))

    session = SimpleNamespace(
        interrupt=interrupt,
        clear_user_turn=clear_user_turn,
        generate_reply=generate_reply,
    )
    conversation = LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock(), presentation=controller
    )
    conversation._session = session
    conversation._expressive_agent = agent
    conversation._started = True
    conversation._connection = Mock(is_owner=Mock(return_value=True))
    event = SimpleNamespace(identity="owner")
    try:
        await conversation._on_text_input(session, TextInputEvent(text="挥挥手", participant=event))
        await controller._user_task
        wave = controller._cue
        assert controller.acknowledge(wave.turn_id, wave.gesture_id, "started")
        await conversation._on_text_input(
            session, TextInputEvent(text="停下动作，安静听我说。", participant=event)
        )
        await entered.wait()
        assert calls == [
            "interrupt",
            "clear",
            ("reply", "挥挥手"),
            "interrupt",
            "clear",
            ("reply", "停下动作，安静听我说。"),
        ]
        assert [item.role for item in history.items] == ["user", "user"]
        assert [item.text_content for item in history.items] == [
            "挥挥手",
            "停下动作，安静听我说。",
        ]
        assert contexts[-1].items[1:-1] == history.items
        assert "本次动作状态：pending。" in contexts[-1].items[-1].text_content
        assert "本轮仍在执行或待执行的动作目标：尚未确认。" in (contexts[-1].items[-1].text_content)
        assert "以最后一条用户消息为当前请求" in contexts[-1].items[0].text_content
        assert "不能先答应或补做旧动作再停止" in contexts[-1].items[0].text_content
        assert "本轮仍在执行或待执行的动作目标：wave。" not in (contexts[-1].items[-1].text_content)
        assert controller.turn_id != wave.turn_id
        assert controller._command is None
        assert not controller.acknowledge(wave.turn_id, wave.gesture_id, "completed")
        release.set()
        await controller._user_task
        current = agent.generation_context(history)
        expected_state = "none" if stop_decision == "prohibited" else "unavailable"
        assert f"本次动作状态：{expected_state}。" in current.items[-1].text_content
        assert controller._prohibited == "all"
        assert controller._last_command == "none"
        assert controller._cue.intent.gesture == "none"
        assert port.decide_user.await_count == 2
        assert [item.text_content for item in history.items] == [
            "挥挥手",
            "停下动作，安静听我说。",
        ]
    finally:
        release.set()
        await controller.aclose()
