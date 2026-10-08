"""每轮环境与时钟只进入临时模型输入，净历史不累积过期事实或内部协议。"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from livekit.agents import Agent, FlushSentinel, ModelSettings, llm

from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.infrastructure.expressive_agent import ExpressiveAgent


async def source(*items):
    """以原对象模拟 SDK 输出，才能断言本层没有重建工具或用量块。"""
    for item in items:
        yield item


async def test_expressive_agent_preserves_tools_and_refreshes_ephemeral_facts(monkeypatch):
    """跨午夜重新读取时钟；即使历史和定制偏好过时，也不能覆盖实际身份与能力。"""
    clock = Mock(
        now=Mock(
            side_effect=[
                datetime(2026, 10, 7, 15, 59, tzinfo=UTC),
                datetime(2026, 10, 7, 16, 1, tzinfo=UTC),
            ]
        )
    )
    agent = ExpressiveAgent(
        instructions="只用短句回答。",
        tools=[],
        room=Mock(),
        voices={},
        clock=clock,
        environment=AssistantEnvironment(channel="web", tool_names=("calculate",)),
    )
    context = llm.ChatContext()
    context.add_message(role="assistant", content="我只有声音，不能做表情。")
    context.add_message(role="user", content="害羞给我看看")
    call = llm.FunctionCall(name="calculate", arguments='{"expression":"2+3"}', call_id="c")
    context.insert(call)
    before = list(context.items)
    tool = llm.FunctionToolCall(name="calculate", arguments='{"expression":"2+3"}', call_id="c")
    chunk = llm.ChatChunk(id="r", delta=llm.ChoiceDelta(tool_calls=[tool]))
    flush = FlushSentinel()
    items = ["[xiaoya:neutral|none]纯净正文。", chunk, flush]
    captured = []

    def generate(_agent, generation_context, *_args):
        """检查公开模型输入，不使用 SDK activity 私有字段或实际网络。"""
        captured.append(generation_context)
        return source(*items)

    monkeypatch.setattr(Agent.default, "llm_node", generate)
    for _ in range(2):
        result = [item async for item in agent.llm_node(context, [], ModelSettings())]
        assert result[0] == "[xiaoya:neutral|none]纯净正文。"
        forwarded = next(item for item in result if isinstance(item, llm.ChatChunk))
        assert forwarded.delta.tool_calls == [tool]
    assert agent.instructions == "只用短句回答。"
    assert context.items == before
    assert all(copy.items[:-1] == before for copy in captured)
    facts = [copy.messages()[-1].text_content for copy in captured]
    assert "2026-10-07" in facts[0] and "2026-10-08" in facts[1]
    assert all("小芽" in fact and "害羞" in fact and "calculate" in fact for fact in facts)
    assert all("Asia/Shanghai" in fact for fact in facts)
    assert clock.now.call_count == 2


def test_environment_is_replaced_before_future_generation():
    """未知客户端和终端不冒充网页，装配完成后新副本才使用确认后的能力。"""
    agent = ExpressiveAgent(instructions="偏好", tools=[], room=Mock(), voices={})
    assert agent.environment.channel == "unknown"
    environment = AssistantEnvironment(channel="console")
    agent.set_environment(environment)
    assert agent.environment is environment
    facts = agent.generation_context(llm.ChatContext()).messages()[-1].text_content
    assert "终端" in facts


def test_invalid_clock_fails_without_inventing_time():
    """错误时钟不能被静默替换为无时区的本机时间或启动时缓存值。"""
    agent = ExpressiveAgent(
        instructions="偏好",
        tools=[],
        room=Mock(),
        voices={},
        clock=Mock(now=Mock(return_value=datetime(2026, 10, 7))),
    )
    with pytest.raises(ValueError):
        agent.generation_context(llm.ChatContext())
