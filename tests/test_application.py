"""用例只验证业务顺序和失败边界，避免依赖云端响应。"""

from unittest.mock import AsyncMock, Mock

import pytest

from xiaoya.application.ports import VoiceConversation
from xiaoya.application.start_conversation import StartVoiceConversation
from xiaoya.domain.assistant import AssistantProfile


async def test_conversation_starts_before_greeting() -> None:
    """顺序用跨方法调用记录验证，防止未连接时提前播报。"""
    conversation = Mock(spec=VoiceConversation)
    profile = AssistantProfile()
    await StartVoiceConversation(conversation=conversation, profile=profile).execute()
    assert conversation.mock_calls == [
        ("start", (profile,), {}),
        ("say", (profile.greeting,), {}),
    ]
    conversation.start.assert_awaited_once_with(profile)
    conversation.say.assert_awaited_once_with(profile.greeting)


async def test_start_failure_prevents_greeting() -> None:
    """启动失败必须阻止后续操作，并保留原始异常供 Job 判定失败。"""
    conversation = Mock(spec=VoiceConversation)
    conversation.start = AsyncMock(side_effect=ConnectionError("无法连接房间"))
    with pytest.raises(ConnectionError, match="无法连接房间"):
        await StartVoiceConversation(conversation, AssistantProfile()).execute()
    conversation.say.assert_not_awaited()


async def test_greeting_failure_is_not_swallowed() -> None:
    """播报失败也属于用例失败，不能留下成功启动的假象。"""
    conversation = Mock(spec=VoiceConversation)
    conversation.say = AsyncMock(side_effect=RuntimeError("合成失败"))
    with pytest.raises(RuntimeError, match="合成失败"):
        await StartVoiceConversation(conversation, AssistantProfile()).execute()
