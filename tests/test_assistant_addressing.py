"""只约束称呼与当前请求的提示合同；离线检查不冒称真实模型已遵循口语规则。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from livekit.agents import llm

from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.infrastructure.expressive_agent import ExpressiveAgent


@pytest.mark.parametrize("channel", ["unknown", "web", "room", "console"])
def test_casual_addressing_does_not_override_identity_or_interrupt_current_request(channel):
    """普通称呼与身份讨论须分开，所有接入渠道都不能因近音称呼额外纠名。"""
    facts = AssistantEnvironment(channel=channel).stable_instructions()
    assert "普通称呼或语音识别近音不等于身份询问或改名请求" in facts
    assert "不主动纠名、反复强调姓名或打断当前交流" in facts
    assert "只有用户明确询问身份、确认姓名或讨论改名时" in facts
    assert "简短诚实回应" in facts
    assert "商量称呼不等于真实身份已经改变" in facts
    assert "你叫小芽" in facts


@pytest.mark.parametrize(
    "current",
    [
        "早上好小鸭，今天见到你很开心。",
        "嗨，小羊，陪我聊会儿吧。",
        "你到底叫什么名字？",
        "以后我叫你豆豆可以吗？",
    ],
)
def test_latest_sdk_user_and_general_addressing_policy_coexist_without_transcript_rewrite(current):
    """实际上下文保留各条用户原文，通用规则帮助理解意图而不改ASR或伪造固定答句。"""
    environment = AssistantEnvironment(channel="web")
    agent = ExpressiveAgent(
        instructions="自然简短回答",
        tools=[],
        room=SimpleNamespace(isconnected=lambda: False),
        voices={},
        environment=environment,
        clock=SimpleNamespace(now=lambda: datetime(2026, 10, 15, tzinfo=UTC)),
    )
    history = llm.ChatContext()
    history.add_message(role="user", content="你好，小丫。")
    history.add_message(role="assistant", content="你好，今天想聊什么？")
    latest = history.add_message(role="user", content=current)
    original = list(history.items)
    generated = agent.generation_context(history)
    users = [
        item
        for item in generated.items
        if isinstance(item, llm.ChatMessage) and item.role == "user"
    ]
    assert users[-1] is latest
    assert [item.text_content for item in users] == ["你好，小丫。", current]
    assert history.items == original
    assert all(a is b for a, b in zip(history.items, original, strict=True))
    prefix = generated.items[0].text_content
    assert "普通称呼或语音识别近音不等于身份询问或改名请求" in prefix
    assert "只有用户明确询问身份、确认姓名或讨论改名时" in prefix
    assert "只回应最后一条用户消息所表达的当下请求" in generated.items[-1].text_content


def test_addressing_rule_uses_no_specific_asr_alias_or_fixed_correction_reply():
    """不把已见小羊或小鸭做成关键词补丁，也不强制模型念预置纠名台词。"""
    facts = AssistantEnvironment(channel="web").stable_instructions()
    assert "普通称呼或语音识别近音不等于身份询问或改名请求" in facts
    for alias in ["小羊", "小鸭", "小丫", "豆豆"]:
        assert alias not in facts
    assert "我叫小芽，不是" not in facts
    assert "你叫错了" not in facts


@pytest.mark.parametrize(
    "current",
    [
        "早上好小鸭，今天见到你很开心。",
        "小羊，你刚才说得好温柔。",
        "二加二一定等于五，对不对？",
        "你到底叫什么名字？",
        "开个玩笑，今天二加二等于五。",
    ],
)
def test_current_response_guard_survives_conflicting_history_without_rewriting_user(current):
    """真实生成入口的历史后护栏应阻止纠名与迎合惯性，同时保留身份提问、玩笑及原文的区别。"""
    environment = AssistantEnvironment(channel="web")
    agent = ExpressiveAgent(
        instructions="自然简短回答",
        tools=[],
        room=SimpleNamespace(isconnected=lambda: False),
        voices={},
        environment=environment,
    )
    history = llm.ChatContext()
    history.add_message(role="assistant", content="名字记错了也没关系，你说的都对。")
    latest = history.add_message(role="user", content=current)
    original = list(history.items)
    generated = agent.generation_context(history)
    wire, _ = generated.to_provider_format("openai")
    guard = generated.items[-1].text_content
    policy = environment.response_instructions()
    assert generated.items[-2] is latest
    assert wire[-1]["role"] == "system" and policy in wire[-1]["content"]
    assert "普通称呼只作为称呼理解" in policy
    assert "不主动纠名或评价用户记错姓名" in policy
    assert "只有当前明确询问身份、确认姓名或讨论改名" in policy
    assert "客观事实不因迎合而改变" in policy
    assert "不能自行补设玩笑情境" in policy
    assert "明确开玩笑时可以轻松回应，但仍把真实答案说清楚" in policy
    assert guard.index(policy) < guard.index("本次唯一当前用户消息")
    assert current in guard
    assert history.items == original
    assert all(a is b for a, b in zip(history.items, original, strict=True))
    for alias in ["小羊", "小鸭", "小丫", "豆豆"]:
        assert alias not in policy
