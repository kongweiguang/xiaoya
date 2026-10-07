"""在公开节点边界验证表现附加协议不污染工具、历史或已播放音频。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import Agent, APIConnectOptions, FlushSentinel, ModelSettings, llm

from xiaoya.domain.delivery import DeliveryIntent, SpeechSegment
from xiaoya.infrastructure import livekit_conversation
from xiaoya.infrastructure.expressive_agent import ExpressiveAgent
from xiaoya.infrastructure.settings import Settings


class NodeAgent(ExpressiveAgent):
    """只替换公开 session 属性，避免测试依赖 SDK 的私有 activity 字段。"""

    def __init__(self, *, voices=None):
        """会话与流对象保持内存实现，节点本身仍是生产代码。"""
        super().__init__(instructions="测试", tools=[], room=Mock(), voices=voices or {})
        self.test_session = SimpleNamespace(
            current_speech=None,
            conn_options=SimpleNamespace(tts_conn_options=APIConnectOptions()),
            output=SimpleNamespace(audio=None, transcription=None),
        )

    @property
    def session(self):
        """使用 SDK 的公开依赖面模拟播放授权，而不伪造内部状态。"""
        return self.test_session


async def source(*items):
    """可按任意分块喂入节点，末尾不补隐式字符。"""
    for item in items:
        yield item


class SynthesisStream:
    """失败位置可控，用于区别首帧前回退与发声后的不可重放边界。"""

    def __init__(self, items):
        """事件序列同时包含正常帧和错误，避免用 HTTP 成功代替音频断言。"""
        self.items = items
        self.closed = False

    async def __aenter__(self):
        """模拟 SDK 公开 ChunkedStream 的资源上下文。"""
        return self

    async def __aexit__(self, *args):
        """取消与合成错误也必须释放当前请求。"""
        self.closed = True

    async def __aiter__(self):
        """异常作为实际读取结果出现，覆盖已开始播放后的失败。"""
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            yield SimpleNamespace(frame=item)


async def test_streaming_node_preserves_tool_metadata_and_boundaries(monkeypatch):
    """有限标记可重组文本，但工具参数、扩展字段及用量块不能被解析器吞掉。"""
    tool = llm.FunctionToolCall(name="calculate", arguments='{"x":2}', call_id="call-1")
    chunks = [
        llm.ChatChunk(id="r", delta=llm.ChoiceDelta(content="[happy|nod]你好。")),
        llm.ChatChunk(
            id="r",
            delta=llm.ChoiceDelta(content="下一步", tool_calls=[tool], extra={"opaque": "kept"}),
        ),
        llm.ChatChunk(
            id="r", usage=llm.CompletionUsage(completion_tokens=2, prompt_tokens=3, total_tokens=5)
        ),
    ]
    monkeypatch.setattr(Agent.default, "llm_node", lambda *args: source(*chunks))
    result = [item async for item in NodeAgent().llm_node(llm.ChatContext(), [], ModelSettings())]
    assert [item for item in result if isinstance(item, str)] == [
        "[happy|nod]你好。",
        "[happy|none]下一步",
    ]
    assert sum(isinstance(item, FlushSentinel) for item in result) == 2
    forwarded = [item for item in result if isinstance(item, llm.ChatChunk)]
    assert forwarded[0].delta.content is None
    assert forwarded[0].delta.tool_calls == [tool]
    assert forwarded[0].delta.extra == {"opaque": "kept"}
    assert forwarded[1].usage.total_tokens == 5
    assert chunks[1].delta.content == "下一步"


async def test_transcription_strips_headers_before_spoken_history():
    """同步器只收到净正文，真实当前 handle 才能绑定到对应句段。"""
    agent = NodeAgent()
    handle = Mock(id="reply-1", interrupted=False, done=Mock(return_value=False))
    agent.test_session.current_speech = handle
    agent._delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock())
    agent._synchronizer = Mock(barrier=AsyncMock())
    result = [
        item
        async for item in agent.transcription_node(
            source("[gent", "le|nod]别担心。"), ModelSettings()
        )
    ]
    assert result == ["别担心。"]
    agent._synchronizer.barrier.assert_awaited_once()
    agent._delivery.bind_segment.assert_awaited_once_with(
        "reply-1", DeliveryIntent("gentle", "nod")
    )
    handle.add_done_callback.assert_called_once()
    agent._delivery.cancel_reply.assert_not_awaited()
    agent._speech_done(handle)
    agent._delivery.cancel_reply.assert_not_called()


@pytest.mark.parametrize("interrupted,done", [(True, False), (False, True)])
async def test_stale_transcription_cannot_write_into_newer_reply(interrupted, done):
    """旧回调返回时丢弃旧正文，而不是写入已经切到新回复的字幕输出。"""
    agent = NodeAgent()
    handle = Mock(id="old", interrupted=interrupted, done=Mock(return_value=done))
    agent.test_session.current_speech = handle
    agent._delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock())
    result = [
        item
        async for item in agent.transcription_node(source("[happy|wave]旧内容。"), ModelSettings())
    ]
    assert result == []
    agent._delivery.bind_segment.assert_not_awaited()
    if interrupted:
        agent._delivery.cancel_reply.assert_awaited_once_with("old")


async def test_agent_does_not_repeat_service_fallback_before_or_after_audio():
    """服务已拥有回退预算，Agent 在首帧前和发声后都不能再重放同一句。"""
    failed = SynthesisStream([RuntimeError("offline")])
    natural = SynthesisStream(["natural-frame"])
    voices = {
        "happy": Mock(synthesize=Mock(return_value=failed)),
        "neutral": Mock(synthesize=Mock(return_value=natural)),
    }
    agent = NodeAgent(voices=voices)
    with pytest.raises(RuntimeError, match="offline"):
        _ = [
            frame
            async for frame in agent._synthesize(SpeechSegment("你好。", DeliveryIntent("happy")))
        ]
    assert failed.closed and not natural.closed
    assert voices["happy"].synthesize.call_args.kwargs["conn_options"].max_retry == 0
    voices["neutral"].synthesize.assert_not_called()
    voices["neutral"].synthesize.reset_mock()
    voices["happy"].synthesize.return_value = SynthesisStream(["frame", RuntimeError("mid-stream")])
    frames = []
    with pytest.raises(RuntimeError, match="mid-stream"):
        async for frame in agent._synthesize(SpeechSegment("你好。", DeliveryIntent("happy"))):
            frames.append(frame)
    assert frames == ["frame"]
    voices["neutral"].synthesize.assert_not_called()


async def test_cancelled_voice_does_not_retry_natural():
    """打断是控制信号而不是合成故障，不能在取消后启动新请求。"""
    stream = SynthesisStream([asyncio.CancelledError()])
    voices = {"gentle": Mock(synthesize=Mock(return_value=stream)), "neutral": Mock()}
    with pytest.raises(asyncio.CancelledError):
        _ = [
            frame
            async for frame in NodeAgent(voices=voices)._synthesize(
                SpeechSegment("没关系。", DeliveryIntent("concerned"))
            )
        ]
    assert stream.closed
    voices["neutral"].synthesize.assert_not_called()


async def test_close_releases_publisher_when_synchronizer_fails():
    """装饰输出的单点失败不能泄漏 RPC、队列或下一次 Job 的资源。"""
    agent = NodeAgent()
    delivery = Mock(aclose=AsyncMock())
    agent._delivery = delivery
    agent._synchronizer = Mock(aclose=AsyncMock(side_effect=RuntimeError("close")))
    with pytest.raises(RuntimeError, match="close"):
        await agent.aclose()
    delivery.aclose.assert_awaited_once()
    assert agent._delivery is None and agent._synchronizer is None
    await agent.aclose()
    delivery.aclose.assert_awaited_once()


async def test_early_sdk_entry_still_installs_after_room_connect(monkeypatch):
    """SDK 并发连接竞态不能让整个会话永久绕过表现，兜底也不能重复包装音频。"""
    from xiaoya.infrastructure import expressive_agent

    agent = NodeAgent()
    agent._room.isconnected.return_value = False
    agent.test_session.output.audio = Mock()
    publisher = Mock()
    sync = Mock(audio_output=Mock(), text_output=Mock())
    factory = Mock(return_value=publisher)
    synchronizer_factory = Mock(return_value=sync)
    monkeypatch.setattr(expressive_agent, "DeliveryTextOutput", factory)
    monkeypatch.setattr(expressive_agent, "TranscriptSynchronizer", synchronizer_factory)
    await agent.on_enter()
    factory.assert_not_called()
    agent._room.isconnected.return_value = True
    await agent.enable_delivery()
    await agent.enable_delivery()
    factory.assert_called_once_with(agent._room)
    synchronizer_factory.assert_called_once()
    assert agent.test_session.output.audio is sync.audio_output
    assert agent.test_session.output.transcription is sync.text_output


async def test_enabled_adapter_uses_immutable_private_voice_presets(
    monkeypatch, private_settings: Settings
):
    """开启表现不引入第二语音出口，所有有限预设继续使用同一私有音色。"""
    session = Mock(start=AsyncMock(), say=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(livekit_conversation, "AgentSession", Mock(return_value=session))
    monkeypatch.setattr(livekit_conversation.inference, "TurnDetector", Mock())
    monkeypatch.setattr(
        livekit_conversation.openai, "STT", Mock(return_value=Mock(aclose=AsyncMock()))
    )
    monkeypatch.setattr(
        livekit_conversation.openai, "LLM", Mock(return_value=Mock(aclose=AsyncMock()))
    )
    factory = Mock(side_effect=lambda **kwargs: Mock(aclose=AsyncMock()))
    monkeypatch.setattr(livekit_conversation.openai, "TTS", factory)
    adapter = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(isconnected=Mock(return_value=False)),
        settings=replace(private_settings, expressive_enabled=True),
        vad=Mock(),
    )
    from xiaoya.domain.assistant import AssistantProfile

    await adapter.start(AssistantProfile())
    await adapter.say("你好。")
    assert isinstance(session.start.call_args.kwargs["agent"], ExpressiveAgent)
    assert session.start.call_args.kwargs["room_options"].text_output.sync_transcription is False
    assert livekit_conversation.AgentSession.call_args.kwargs["tts_text_transforms"] == []
    assert [call.kwargs.get("instructions") for call in factory.call_args_list] == [
        None,
        "开心自然地说话。",
        "轻柔温和地说话。",
    ]
    assert all(
        call.kwargs["base_url"] == private_settings.tts_base_url for call in factory.call_args_list
    )
    session.say.assert_awaited_once_with("[happy|wave]你好。")
    await adapter.close()
    session.aclose.assert_awaited_once()


async def test_markdown_is_filtered_only_after_control_header():
    """实际 SDK 会把控制头加括号正文识别成链接，禁前置过滤后必须仍选开心且正文净化。"""
    from livekit.agents.voice.transcription import text_transforms

    raw = "[happy|wave](Hello) **Good morning**. 😀"
    sdk_default = "".join([part async for part in text_transforms.filter_markdown(source(raw))])
    assert "happy|wave" in sdk_default and "[happy|wave]" not in sdk_default
    voice = Mock(synthesize=Mock(return_value=SynthesisStream(["frame"])))
    agent = NodeAgent(voices={"happy": voice})
    assert [frame async for frame in agent.tts_node(source(raw), ModelSettings())] == ["frame"]
    body = voice.synthesize.call_args.args[0]
    assert "Hello" in body and "Good morning" in body
    assert "happy" not in body and "|" not in body and "**" not in body and "😀" not in body
