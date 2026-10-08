"""外部 SDK 边界保持内存实现，验证唯一链路、启动失败和 Job 所有权。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock

import pytest
from livekit import rtc

from xiaoya.domain.assistant import AssistantEnvironment, AssistantProfile
from xiaoya.infrastructure import livekit_conversation
from xiaoya.infrastructure.delivery_output import DeliveryState
from xiaoya.infrastructure.settings import Settings


class MemoryAgent:
    """只替换外部会话连接，保留环境确认和共享状态的实际生命周期约束。"""

    def __init__(self, **kwargs) -> None:
        """记录装配事实，不能以 Mock 的任意属性真值伪造环境能力。"""
        self.environment = kwargs["environment"]
        self.instructions = kwargs["instructions"]
        self.delivery_snapshot = None
        self.enable_delivery = AsyncMock(side_effect=self.install)
        self.aclose = AsyncMock()

    async def install(self, endpoint) -> None:
        """状态实例必须来自会话所有者，测试也不能另外生成一套表现身份。"""
        self.delivery_snapshot = DeliveryState(instance=endpoint.instance, revision=3)

    def set_environment(self, environment: AssistantEnvironment) -> None:
        """每次替换完整事实快照，以检测装配是否在真实启动完成后确认渠道。"""
        self.environment = environment


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """不连接模型或房间，只替换公开构造器和 IO，资源计数仍逐实例记录。"""
    session = Mock(start=AsyncMock(), say=AsyncMock(), aclose=AsyncMock())
    connection = Mock(bind=AsyncMock(), aclose=AsyncMock(), is_owner=Mock(return_value=True))
    session_factory = Mock(return_value=session)
    stt = Mock(aclose=AsyncMock())
    llm = Mock(aclose=AsyncMock())
    voices = [Mock(aclose=AsyncMock()) for _ in range(3)]
    stt_factory = Mock(return_value=stt)
    llm_factory = Mock(return_value=llm)
    tts_factory = Mock(side_effect=voices)
    detector_factory = Mock()
    agent_factory = Mock(side_effect=lambda **kwargs: MemoryAgent(**kwargs))
    monkeypatch.setattr(livekit_conversation, "AgentSession", session_factory)
    monkeypatch.setattr(livekit_conversation, "LocalStreamingSTT", stt_factory)
    monkeypatch.setattr(livekit_conversation.openai, "LLM", llm_factory)
    monkeypatch.setattr(livekit_conversation.openai, "TTS", tts_factory)
    monkeypatch.setattr(livekit_conversation.inference, "TurnDetector", detector_factory)
    monkeypatch.setattr(livekit_conversation, "ExpressiveAgent", agent_factory)
    monkeypatch.setattr(
        livekit_conversation, "ConversationConnection", Mock(return_value=connection)
    )
    room = Mock(metadata='{"xiaoya":{"v":1,"client":"web"}}')
    room.remote_participants = {"owner": SimpleNamespace(identity="owner", sid="PA_owner")}
    return SimpleNamespace(
        session=session,
        connection=connection,
        room=room,
        stt=stt,
        llm=llm,
        voices=voices,
        models=[stt, llm, *voices],
        session_factory=session_factory,
        stt_factory=stt_factory,
        llm_factory=llm_factory,
        tts_factory=tts_factory,
        detector_factory=detector_factory,
        agent_factory=agent_factory,
    )


async def test_constructor_is_inert_and_job_close_before_start_needs_no_models(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """Job 可先登记关闭回调，构造阶段不分配无法被宿主管理的模型资源。"""
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock()
    )
    assert conversation._models == [] and conversation._session is None
    boundary.stt_factory.assert_not_called()
    boundary.session_factory.assert_not_called()
    await conversation.close()
    boundary.connection.aclose.assert_awaited_once()
    for model in boundary.models:
        model.aclose.assert_not_awaited()


async def test_single_expressive_pipeline_has_explicit_private_presets_and_clean_start_order(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """控制解析后才过滤正文，唯一 neutral TTS 直接交给会话；注册后绑定再放行就绪。"""
    vad = Mock()
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=vad
    )
    await conversation.start(AssistantProfile())
    await conversation.say("你好。")
    arguments = boundary.session_factory.call_args.kwargs
    assert arguments["stt"] is boundary.stt and arguments["llm"] is boundary.llm
    assert arguments["tts"] is boundary.voices[0]
    assert arguments["tts_text_transforms"] == [] and arguments["vad"] is vad
    assert arguments["turn_handling"]["interruption"] == {"mode": "vad"}
    boundary.detector_factory.assert_called_once_with(version="v1-mini")
    boundary.stt_factory.assert_called_once_with(
        model=private_settings.stt_model,
        language="zh",
        base_url=private_settings.stt_base_url,
        api_key="not-required",
        vad_model=vad,
    )
    boundary.llm_factory.assert_called_once_with(
        model=private_settings.llm_model,
        base_url=private_settings.llm_base_url,
        api_key=private_settings.llm_api_key,
        extra_body={"thinking": {"type": "disabled"}},
    )
    assert [call.kwargs.get("instructions") for call in boundary.tts_factory.call_args_list] == [
        None,
        "开心自然地说话。",
        "轻柔温和地说话。",
    ]
    assert all(
        call.kwargs["api_key"] == "not-required" for call in boundary.tts_factory.call_args_list
    )
    options = boundary.session.start.call_args.kwargs["room_options"]
    assert options.close_on_disconnect is False and options.text_output.sync_transcription is False
    agent = boundary.session.start.call_args.kwargs["agent"]
    assert agent.environment.channel == "web"
    agent.enable_delivery.assert_awaited_once_with(conversation._snapshot_endpoint)
    boundary.connection.bind.assert_awaited_once_with(boundary.session.room_io)
    boundary.session.say.assert_awaited_once_with("[xiaoya:happy|wave]你好。")
    await asyncio.gather(conversation.close(), conversation.close())
    boundary.session.aclose.assert_awaited_once()
    for model in boundary.models:
        model.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "failure_at", ["stt", "llm", "neutral", "happy", "gentle", "turn", "session"]
)
async def test_partial_model_construction_failure_closes_exactly_owned_resources(
    failure_at: str,
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """每项模型成功即登记，后续构造失败不会泄漏客户端或被关闭异常掩盖。"""
    if failure_at in {"stt", "llm", "turn", "session"}:
        factory = {
            "stt": boundary.stt_factory,
            "llm": boundary.llm_factory,
            "turn": boundary.detector_factory,
            "session": boundary.session_factory,
        }[failure_at]
        factory.side_effect = RuntimeError("创建失败")
    else:
        index = {"neutral": 0, "happy": 1, "gentle": 2}[failure_at]
        boundary.tts_factory.side_effect = [*boundary.voices[:index], RuntimeError("创建失败")]
    mcp = Mock(close=AsyncMock(), tools=AsyncMock())
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock(), mcp=mcp
    )
    with pytest.raises(RuntimeError, match="创建失败"):
        await conversation.start(AssistantProfile())
    expected = {"stt": 0, "llm": 1, "neutral": 2, "happy": 3, "gentle": 4, "turn": 5, "session": 5}[
        failure_at
    ]
    assert len(conversation._models) == expected
    for model in boundary.models[:expected]:
        model.aclose.assert_awaited_once()
    for model in boundary.models[expected:]:
        model.aclose.assert_not_awaited()
    mcp.close.assert_awaited_once()
    boundary.session.say.assert_not_awaited()
    await conversation.close()


@pytest.mark.parametrize("failure", ["mcp", "session", "owner", "snapshot"])
async def test_start_failure_never_says_greeting_or_notifies_runtime_completion(
    failure: str,
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """外部初始化失败按原异常传播，已经登记的模型仍全部关闭，不能假装运行态成功。"""
    mcp = Mock(close=AsyncMock(), tools=AsyncMock(return_value=[]))
    if failure == "mcp":
        mcp.tools.side_effect = RuntimeError("初始化失败")
    elif failure == "session":
        boundary.session.start.side_effect = RuntimeError("初始化失败")
    elif failure == "owner":
        boundary.connection.bind.side_effect = RuntimeError("初始化失败")
    else:
        boundary.room.local_participant.register_rpc_method.side_effect = RuntimeError("初始化失败")
    terminal = Mock()
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock(), mcp=mcp, on_terminal=terminal
    )
    with pytest.raises(RuntimeError, match="初始化失败"):
        await conversation.start(AssistantProfile())
    terminal.assert_not_called()
    boundary.session.say.assert_not_awaited()
    for model in boundary.models:
        model.aclose.assert_awaited_once()
    mcp.close.assert_awaited_once()
    assert not conversation._started


@pytest.mark.parametrize("event_name", ["close", "error"])
async def test_close_during_start_propagates_failure(
    event_name: str,
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """SDK 并发结束必须阻止启动成功，不能让开场白进入已经关闭的资源。"""

    async def stop_during_start(**_kwargs) -> None:
        """事件发生在 SDK start 返回前，避免依赖调度偶然覆盖竞态。"""
        callbacks = {call.args[0]: call.args[1] for call in boundary.session.on.call_args_list}
        callbacks[event_name](SimpleNamespace(error=SimpleNamespace(recoverable=False)))

    boundary.session.start.side_effect = stop_during_start
    terminal = Mock()
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock(), on_terminal=terminal
    )
    with pytest.raises(RuntimeError, match="启动完成前已关闭"):
        await conversation.start(AssistantProfile())
    terminal.assert_not_called()
    boundary.session.aclose.assert_awaited_once()


@pytest.mark.parametrize("event_name", ["close", "error", "connection"])
async def test_runtime_terminal_closes_once_then_notifies_job(
    event_name: str,
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """同步 SDK 事件只调度回收，宿主通知发生在模型和协议资源释放之后。"""
    closed = []
    terminal = Mock(side_effect=lambda: closed.append("terminal"))
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock(), on_terminal=terminal
    )
    await conversation.start(AssistantProfile())
    boundary.session.aclose.side_effect = lambda: closed.append("session")
    boundary.connection.aclose.side_effect = lambda: closed.append("connection")
    for index, model in enumerate(boundary.models):
        model.aclose.side_effect = lambda index=index: closed.append(index)
    callbacks = {call.args[0]: call.args[1] for call in boundary.session.on.call_args_list}
    callbacks["error"](SimpleNamespace(error=SimpleNamespace(recoverable=True)))
    assert conversation._close_task is None
    if event_name == "connection":
        conversation._on_connection_end()
    else:
        callbacks[event_name](SimpleNamespace(error=SimpleNamespace(recoverable=False)))
    await asyncio.gather(conversation.close(), conversation.close())
    callbacks["close"](Mock())
    await conversation.close()
    assert closed == ["connection", "session", 4, 3, 2, 1, 0, "terminal"]
    terminal.assert_called_once()
    assert not conversation._snapshot_endpoint.registered


async def test_cancelled_close_waiter_does_not_cancel_cleanup(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """调用者取消等待不能中断实际释放；下一位关闭者共享同一清理任务。"""
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_close() -> None:
        """用明确等待控制回收中间窗口，取消断言无需真实网络或计时碰巧。"""
        entered.set()
        await release.wait()

    boundary.session.aclose.side_effect = delayed_close
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock()
    )
    await conversation.start(AssistantProfile())
    waiter = asyncio.create_task(conversation.close())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await conversation.close()
    boundary.session.aclose.assert_awaited_once()
    for model in boundary.models:
        model.aclose.assert_awaited_once()


async def test_owner_wait_cancel_releases_resources_and_revokes_snapshot(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """启动取消仍回收全部已创建资源，不能保留已登记但没有用户的就绪入口。"""
    entered = asyncio.Event()

    async def wait_for_owner(_room_io) -> None:
        """只阻塞用户绑定，保证取消发生在快照登记之后。"""
        entered.set()
        await asyncio.Event().wait()

    boundary.connection.bind.side_effect = wait_for_owner
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock()
    )
    task = asyncio.create_task(conversation.start(AssistantProfile()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not conversation._snapshot_endpoint.registered
    for model in boundary.models:
        model.aclose.assert_awaited_once()


async def test_close_failure_does_not_skip_other_resources(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """局部 SDK 释放失败不能跳过模型或 MCP，重复关闭仍只消费同一次失败。"""
    mcp = Mock(close=AsyncMock(), tools=AsyncMock(return_value=[]))
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock(), mcp=mcp
    )
    await conversation.start(AssistantProfile())
    boundary.session.aclose.side_effect = RuntimeError("回收失败")
    with pytest.raises(RuntimeError, match="回收失败"):
        await conversation.close()
    for model in boundary.models:
        model.aclose.assert_awaited_once()
    mcp.close.assert_awaited_once()


async def test_snapshot_readiness_requires_binding_and_live_owner(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """登记并不等于 ready，过期用户 SID、未绑定和关闭状态都不能读取授权快照。"""

    async def inspect_pending(_room_io) -> None:
        """读取真正登记的 handler，避免用 mock ready 值代替端点行为。"""
        handler = boundary.room.local_participant.register_rpc_method.call_args.args[1]
        with pytest.raises(rtc.RpcError):
            await handler(SimpleNamespace(caller_identity="owner"))

    boundary.connection.bind.side_effect = inspect_pending
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock()
    )
    await conversation.start(AssistantProfile())
    method, handler = boundary.room.local_participant.register_rpc_method.call_args.args
    assert method == "xiaoya.getDeliverySnapshot"
    payload = json.loads(await handler(SimpleNamespace(caller_identity="owner")))
    assert payload["instance"] == conversation._snapshot_endpoint.instance
    assert payload["revision"] == 3 and payload["v"] == 1
    boundary.connection.is_owner.return_value = False
    with pytest.raises(rtc.RpcError):
        await handler(SimpleNamespace(caller_identity="owner"))
    boundary.connection.is_owner.return_value = True
    conversation._schedule_close()
    with pytest.raises(rtc.RpcError):
        await handler(SimpleNamespace(caller_identity="owner"))
    await conversation.close()
    boundary.room.local_participant.unregister_rpc_method.assert_called_once_with(method)


async def test_console_uses_same_voice_without_room_output_or_snapshot(
    private_settings: Settings,
    boundary: SimpleNamespace,
) -> None:
    """Console 的公开 IO 属性明确不存在，声音与协议共用而不等待不存在的房间用户。"""
    type(boundary.session).room_io = PropertyMock(side_effect=RuntimeError("no room IO"))
    conversation = livekit_conversation.LiveKitVoiceConversation(
        room=boundary.room, settings=private_settings, vad=Mock()
    )
    await conversation.start(AssistantProfile())
    agent = boundary.session.start.call_args.kwargs["agent"]
    assert agent.environment.channel == "console"
    agent.enable_delivery.assert_not_awaited()
    boundary.connection.bind.assert_not_awaited()
    boundary.room.local_participant.register_rpc_method.assert_not_called()
    await conversation.say("本机测试。")
    boundary.session.say.assert_awaited_once_with("[xiaoya:happy|wave]本机测试。")
    await conversation.close()


@pytest.mark.parametrize(
    "metadata,channel",
    [
        ('{"xiaoya":{"v":1,"client":"web"}}', "web"),
        ("{}", "room"),
        ('{"xiaoya":{"v":true,"client":"web"}}', "room"),
        ("invalid", "room"),
    ],
)
def test_channel_only_accepts_signed_fixed_metadata(metadata, channel) -> None:
    """任意房名和未验证描述不能作为高优先级模型指令，只有固定网页标识影响渠道。"""
    assert livekit_conversation._room_channel(metadata) == channel
