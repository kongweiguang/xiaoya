"""替换 SDK 外部边界，验证业务文案与关闭语义确实传给适配器。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from xiaoya.application.assistant_tools import AssistantTools
from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure import livekit_conversation
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter, SystemClock
from xiaoya.infrastructure.settings import Settings


async def test_adapter_passes_rules_and_releases_session(
    monkeypatch: pytest.MonkeyPatch, private_settings: Settings
) -> None:
    """显式模型参数不继承公共配置；提前合成仍遵守轮次、打断及资源释放边界。"""
    session = Mock(start=AsyncMock(), say=AsyncMock(), aclose=AsyncMock())
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(livekit_conversation, "AgentSession", session_factory)
    detector = Mock()
    detector_factory = Mock(return_value=detector)
    monkeypatch.setattr(livekit_conversation.inference, "TurnDetector", detector_factory)
    models = [Mock(aclose=AsyncMock()) for _ in range(3)]
    for name, model in zip(("STT", "LLM", "TTS"), models, strict=True):
        monkeypatch.setattr(livekit_conversation.openai, name, Mock(return_value=model))
    room = Mock()
    vad = Mock()
    profile = AssistantProfile()
    adapter = livekit_conversation.LiveKitVoiceConversation(
        room=room, settings=private_settings, vad=vad
    )

    await adapter.start(profile)
    await adapter.say(profile.greeting)
    await adapter.close()

    session_factory.assert_called_once()
    assert session_factory.call_args.kwargs["vad"] is vad
    assert session_factory.call_args.kwargs["turn_handling"] == {
        "turn_detection": detector,
        "endpointing": {"min_delay": 0.90, "max_delay": 1.10},
        "preemptive_generation": {"enabled": True, "preemptive_tts": True},
        "interruption": {"mode": "vad"},
    }
    detector_factory.assert_called_once_with(version="v1-mini")
    assert session.start.call_args.kwargs["room"] is room
    assert session.start.call_args.kwargs["agent"].instructions == profile.instructions
    session.start.assert_awaited_once()
    session.say.assert_awaited_once_with(profile.greeting)
    assert {call.args[0] for call in session.on.call_args_list} == {"error", "close"}
    assert session.off.call_count == 2
    room.local_participant.publish_track.assert_not_called()
    session.aclose.assert_awaited_once()
    for model in models:
        model.aclose.assert_awaited_once()
    livekit_conversation.openai.STT.assert_called_once_with(
        model=private_settings.stt_model,
        language="zh",
        base_url=private_settings.stt_base_url,
        api_key="not-required",
        use_realtime=False,
    )
    livekit_conversation.openai.LLM.assert_called_once_with(
        model=private_settings.llm_model,
        base_url=private_settings.llm_base_url,
        api_key="not-required",
        extra_body={},
    )
    livekit_conversation.openai.TTS.assert_called_once_with(
        model=private_settings.tts_model,
        voice=private_settings.tts_voice,
        base_url=private_settings.tts_base_url,
        api_key="not-required",
        response_format="wav",
    )


async def test_model_resources_close_even_when_session_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    private_settings: Settings,
) -> None:
    """故障回收路径也必须释放模型客户端，避免长驻进程逐次累积连接。"""
    session = Mock(aclose=AsyncMock(side_effect=RuntimeError("会话关闭失败")))
    monkeypatch.setattr(livekit_conversation, "AgentSession", Mock(return_value=session))
    models = [Mock(aclose=AsyncMock()) for _ in range(3)]
    models[2].aclose.side_effect = RuntimeError("合成客户端关闭失败")
    for name, model in zip(("STT", "LLM", "TTS"), models, strict=True):
        monkeypatch.setattr(livekit_conversation.openai, name, Mock(return_value=model))
    adapter = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock()
    )
    with pytest.raises(RuntimeError, match="合成客户端关闭失败"):
        await adapter.close()
    for model in models:
        model.aclose.assert_awaited_once()


async def test_mcp_failure_prevents_session_start_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
    private_settings: Settings,
) -> None:
    """MCP 是显式声明的能力，失败不能继续发送成功开场白，所有模型也要回收。"""
    session = Mock(start=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(livekit_conversation, "AgentSession", Mock(return_value=session))
    mcp = Mock(tools=AsyncMock(side_effect=RuntimeError("MCP 初始化失败")), close=AsyncMock())
    adapter = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(),
        settings=private_settings,
        vad=Mock(),
        tools=AssistantToolAdapter(AssistantTools(clock=SystemClock())),
        mcp=mcp,
    )
    with pytest.raises(RuntimeError, match="MCP"):
        await adapter.start(AssistantProfile())
    session.start.assert_not_awaited()
    session.aclose.assert_awaited_once()
    mcp.close.assert_awaited_once()


@pytest.fixture
def lifecycle_boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """只替换外部 SDK 调用，保留生产的事件绑定、任务取消与并发关闭语义。"""
    session = Mock(start=AsyncMock(), say=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(livekit_conversation, "AgentSession", Mock(return_value=session))
    monkeypatch.setattr(livekit_conversation.inference, "TurnDetector", Mock())
    models = [Mock(aclose=AsyncMock()) for _ in range(3)]
    for name, model in zip(("STT", "LLM", "TTS"), models, strict=True):
        monkeypatch.setattr(livekit_conversation.openai, name, Mock(return_value=model))
    return SimpleNamespace(session=session, models=models)


async def test_unrecoverable_session_error_closes_audio_resources(
    private_settings: Settings, lifecycle_boundary: SimpleNamespace
) -> None:
    """移除视频回调后仍保留严重错误回收；可恢复错误继续交给 SDK，不能提前挂断。"""
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock()
    )
    await conversation.start(AssistantProfile())
    callbacks = {
        call.args[0]: call.args[1] for call in lifecycle_boundary.session.on.call_args_list
    }
    callbacks["error"](SimpleNamespace(error=SimpleNamespace(recoverable=True)))
    assert conversation._close_task is None
    callbacks["error"](SimpleNamespace(error=SimpleNamespace(recoverable=False)))
    await asyncio.gather(conversation.close(), conversation.close())
    lifecycle_boundary.session.aclose.assert_awaited_once()
    assert lifecycle_boundary.session.off.call_count == 2
    for model in lifecycle_boundary.models:
        model.aclose.assert_awaited_once()


async def test_sdk_close_releases_resources_without_job_callback(
    private_settings: Settings, lifecycle_boundary: SimpleNamespace
) -> None:
    """SDK 主动结束时立即启动回收；晚到的 Job 回调共享结果，不能重复关闭模型。"""
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock()
    )
    await conversation.start(AssistantProfile())
    callbacks = {
        call.args[0]: call.args[1] for call in lifecycle_boundary.session.on.call_args_list
    }
    callbacks["close"](Mock())
    assert conversation._close_task is not None
    await conversation._close_task
    await conversation.close()
    lifecycle_boundary.session.aclose.assert_awaited_once()
    for model in lifecycle_boundary.models:
        model.aclose.assert_awaited_once()


async def test_session_start_failure_stops_greeting_and_releases_resources(
    private_settings: Settings, lifecycle_boundary: SimpleNamespace
) -> None:
    """语音初始化失败仍向用例传播，不能因为网页人物可用而假装会话已启动。"""
    lifecycle_boundary.session.start.side_effect = RuntimeError("语音启动失败")
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock()
    )
    with pytest.raises(RuntimeError, match="语音启动失败"):
        await conversation.start(AssistantProfile())
    await conversation.close()
    lifecycle_boundary.session.say.assert_not_awaited()
    lifecycle_boundary.session.aclose.assert_awaited_once()
    for model in lifecycle_boundary.models:
        model.aclose.assert_awaited_once()


async def test_close_during_start_is_not_reported_as_success(
    private_settings: Settings, lifecycle_boundary: SimpleNamespace
) -> None:
    """启动期间主动结束必须阻止开场白；提前监听 close 保证竞态也能释放模型。"""

    async def stop_during_start(**kwargs: object) -> None:
        """在启动返回前触发真实注册的关闭回调，避免靠偶然的任务时序复现。"""
        callbacks = {
            call.args[0]: call.args[1] for call in lifecycle_boundary.session.on.call_args_list
        }
        callbacks["close"](Mock())

    lifecycle_boundary.session.start.side_effect = stop_during_start
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock()
    )
    with pytest.raises(RuntimeError, match="启动完成前已关闭"):
        await conversation.start(AssistantProfile())
    lifecycle_boundary.session.say.assert_not_awaited()
    lifecycle_boundary.session.aclose.assert_awaited_once()


async def test_cancelled_close_waiter_does_not_cancel_resource_cleanup(
    private_settings: Settings, lifecycle_boundary: SimpleNamespace
) -> None:
    """页面或 Job 取消等待不应取消实际回收，后续等待必须得到同一次清理的结果。"""
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_session_close() -> None:
        """明确控制 SDK 回收等待，验证 shield 不会因调用者取消而中断释放。"""
        entered.set()
        await release.wait()

    lifecycle_boundary.session.aclose.side_effect = delayed_session_close
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(), settings=private_settings, vad=Mock()
    )
    await conversation.start(AssistantProfile())
    waiter = asyncio.create_task(conversation.close())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await conversation.close()
    lifecycle_boundary.session.aclose.assert_awaited_once()
    for model in lifecycle_boundary.models:
        model.aclose.assert_awaited_once()


async def test_builtin_and_mcp_tools_are_added_to_the_agent(
    monkeypatch: pytest.MonkeyPatch,
    private_settings: Settings,
) -> None:
    """装配后的实际 Agent 同时拿到内置与远程工具，而不是只生成未使用的工具列表。"""
    from livekit.agents.llm import function_tool

    session = Mock(start=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(livekit_conversation, "AgentSession", Mock(return_value=session))
    remote = function_tool(
        AsyncMock(),
        raw_schema={"name": "demo__lookup", "parameters": {"type": "object", "properties": {}}},
    )
    mcp = Mock(tools=AsyncMock(return_value=[remote]), close=AsyncMock())
    adapter = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(),
        settings=private_settings,
        vad=Mock(),
        tools=AssistantToolAdapter(AssistantTools(clock=SystemClock())),
        mcp=mcp,
    )
    try:
        await adapter.start(AssistantProfile())
        names = {tool.info.name for tool in session.start.call_args.kwargs["agent"].tools}
        assert "calculate" in names and "demo__lookup" in names
    finally:
        await adapter.close()
    mcp.close.assert_awaited_once()
