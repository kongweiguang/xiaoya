"""在公开节点边界验证表现附加协议不污染工具、历史或已播放音频。"""

import asyncio
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

    def __init__(self, *, voices=None, style="neutral"):
        """节点测试固定控制器的声音选择端口；正文与轮次匹配另由应用层竞争测试验证。"""
        super().__init__(
            instructions="测试",
            tools=[],
            room=Mock(),
            voices=voices or {},
        )
        self.test_session = SimpleNamespace(
            current_speech=None,
            conn_options=SimpleNamespace(tts_conn_options=APIConnectOptions()),
            output=SimpleNamespace(audio=None, transcription=None),
        )
        self._presentation.voice_for = Mock(return_value=DeliveryIntent(style))

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
    """完整短句立即进入独立 SDK 段落，工具参数、扩展字段及用量块仍保持无损。"""
    tool = llm.FunctionToolCall(name="calculate", arguments='{"x":2}', call_id="call-1")
    chunks = [
        llm.ChatChunk(id="r", delta=llm.ChoiceDelta(content="你好。")),
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
    assert [item for item in result if isinstance(item, str)] == ["你好。", "下一步"]
    assert sum(isinstance(item, FlushSentinel) for item in result) == 2
    forwarded = [item for item in result if isinstance(item, llm.ChatChunk)]
    assert forwarded[0].delta.content is None
    assert forwarded[0].delta.tool_calls == [tool]
    assert forwarded[0].delta.extra == {"opaque": "kept"}
    assert forwarded[1].usage.total_tokens == 5
    assert chunks[1].delta.content == "下一步"


async def test_environment_is_ephemeral_and_preserves_tool_history(monkeypatch):
    """当前事实仅放在临时副本，纯正文历史及工具调用身份不累积额外消息。"""
    context = llm.ChatContext()
    context.add_message(role="assistant", content="纯净的已说历史。")
    context.add_message(role="user", content="请好奇地问我一个问题。")
    context.insert(llm.FunctionCall(name="calculate", arguments='{"x":2}', call_id="call-1"))
    before = list(context.items)
    captured = []

    def generate(_agent, generation_context, *_args):
        """捕获公开节点传入的临时副本，不通过私有 SDK activity 读取生成上下文。"""
        captured.append(generation_context)
        return source("你最近有什么新想法呀？")

    monkeypatch.setattr(Agent.default, "llm_node", generate)
    for _ in range(2):
        result = [item async for item in NodeAgent().llm_node(context, [], ModelSettings())]
        assert result[0] == "你最近有什么新想法呀？"
    assert context.items == before
    assert all(generation_context.items[:-1] == before for generation_context in captured)
    assert all("小芽" in copy.messages()[-1].text_content for copy in captured)


async def test_transcription_preserves_plain_spoken_history():
    """同步器只收到净正文；正常handle完成独立收束音频许可，不借文字EOF执行取消。"""
    agent = NodeAgent()
    handle = Mock(id="reply-1", interrupted=False, done=Mock(return_value=False))
    agent.test_session.current_speech = handle
    agent._delivery = Mock(
        bind_segment=AsyncMock(), cancel_reply=AsyncMock(), finish_reply=AsyncMock()
    )
    agent._synchronizer = Mock(barrier=AsyncMock())
    result = [
        item async for item in agent.transcription_node(source("别担", "心。"), ModelSettings())
    ]
    assert result == ["别担心。"]
    agent._synchronizer.barrier.assert_awaited_once()
    agent._delivery.bind_segment.assert_awaited_once_with("reply-1", DeliveryIntent(), "别担心。")
    handle.add_done_callback.assert_called_once()
    agent._delivery.cancel_reply.assert_not_awaited()
    agent._speech_done(handle)
    await asyncio.gather(*agent._tasks)
    agent._delivery.cancel_reply.assert_not_called()
    agent._delivery.finish_reply.assert_awaited_once_with("reply-1")


@pytest.mark.parametrize("interrupted,done", [(True, False), (False, True)])
async def test_stale_transcription_cannot_write_into_newer_reply(interrupted, done):
    """旧回调返回时丢弃旧正文，而不是写入已经切到新回复的字幕输出。"""
    agent = NodeAgent()
    handle = Mock(id="old", interrupted=interrupted, done=Mock(return_value=done))
    agent.test_session.current_speech = handle
    agent._delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock())
    result = [item async for item in agent.transcription_node(source("旧内容。"), ModelSettings())]
    assert result == []
    agent._delivery.bind_segment.assert_not_awaited()
    if interrupted:
        agent._delivery.cancel_reply.assert_awaited_once_with("old")


@pytest.mark.parametrize("change", ["interrupted", "done", "new_handle", "closed"])
@pytest.mark.parametrize("suffix", ["旧后续正文。", "旧后续尾句", "]"])
@pytest.mark.parametrize("delivery_enabled", [True, False])
async def test_transcription_rechecks_permission_after_first_segment(
    change: str, suffix: str, delivery_enabled: bool
) -> None:
    """首段许可不能授权后续文本；普通句段、EOF 尾句与符号在房间和 console 都重新核对。"""
    agent = NodeAgent()
    handle = Mock(id="old", interrupted=False, done=Mock(return_value=False))
    agent.test_session.current_speech = handle
    delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock(), aclose=AsyncMock())
    agent._delivery = delivery if delivery_enabled else None

    async def replaced_source():
        """在首次真实输出后改变公开状态，准确覆盖绑定已成功而取消尚未完成的窗口。"""
        yield "第一句。"
        if change == "interrupted":
            handle.interrupted = True
        elif change == "done":
            handle.done.return_value = True
        elif change == "new_handle":
            agent.test_session.current_speech = Mock(
                id="new", interrupted=False, done=Mock(return_value=False)
            )
        else:
            await agent.aclose()
        yield suffix

    assert [
        item async for item in agent.transcription_node(replaced_source(), ModelSettings())
    ] == ["第一句。"]
    if delivery_enabled:
        delivery.bind_segment.assert_awaited_once_with("old", DeliveryIntent(), "第一句。")
        if change == "interrupted":
            delivery.cancel_reply.assert_awaited_once_with("old")
        else:
            delivery.cancel_reply.assert_not_awaited()


async def test_transcription_keeps_symbols_without_consuming_first_spoken_binding():
    """符号只进入正文，首个可朗读片段才建立身份，后续闭括号不会重新绑定或丢失。"""
    agent = NodeAgent()
    handle = Mock(id="reply", interrupted=False, done=Mock(return_value=False))
    agent.test_session.current_speech = handle
    agent._delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock())
    result = [
        item async for item in agent.transcription_node(source("😊！\n你好。]"), ModelSettings())
    ]
    assert "".join(result) == "😊！\n你好。]"
    agent._delivery.bind_segment.assert_awaited_once_with("reply", DeliveryIntent(), "你好。")


async def test_permission_revoked_while_binding_cannot_commit_text():
    """异步装饰边界返回前许可也可能变化，绑定结果不能替代提交前的最后身份校验。"""
    agent = NodeAgent()
    handle = Mock(id="reply", interrupted=False, done=Mock(return_value=False))
    agent.test_session.current_speech = handle

    async def interrupted_binding(*_args):
        """只改变 SDK 的公开打断事实，不通过私有字段模拟假成功。"""
        handle.interrupted = True

    agent._delivery = Mock(
        bind_segment=AsyncMock(side_effect=interrupted_binding), cancel_reply=AsyncMock()
    )
    assert [
        item async for item in agent.transcription_node(source("不应保存。"), ModelSettings())
    ] == []
    agent._delivery.cancel_reply.assert_awaited_once_with("reply")


@pytest.mark.parametrize("node", ["llm", "tts", "transcription"])
@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_nodes_never_commit_buffered_body(
    monkeypatch: pytest.MonkeyPatch, node: str, failure: BaseException
) -> None:
    """源异常直接传播而不 finish 缓冲，取消和故障都不能把尚未提交的半句补说或补演。"""
    agent = NodeAgent()
    agent.test_session.current_speech = Mock(
        id="reply", interrupted=False, done=Mock(return_value=False)
    )
    agent._delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock())
    synthesized = Mock(return_value=source("unexpected-frame"))
    monkeypatch.setattr(agent, "_synthesize", synthesized)

    async def failed_source():
        """有输入后才失败，LLM 直接透传，合成和字幕不能在清理时补说半句。"""
        yield "尚未提交的半句"
        raise failure

    if node == "llm":

        def generate(*_args):
            """替换公开生成源，保留实际 LLM 节点的协议与错误传播流程。"""
            return failed_source()

        monkeypatch.setattr(Agent.default, "llm_node", generate)
        stream = agent.llm_node(llm.ChatContext(), [], ModelSettings())
    elif node == "tts":
        stream = agent.tts_node(failed_source(), ModelSettings())
    else:
        stream = agent.transcription_node(failed_source(), ModelSettings())
    emitted = []
    with pytest.raises(type(failure)):
        async for item in stream:
            emitted.append(item)
    assert emitted == []
    synthesized.assert_not_called()
    agent._delivery.bind_segment.assert_not_awaited()


async def test_tts_skips_preserved_symbol_segments():
    """文本保真不代表朗读 emoji 或标点，音频入口仍只请求实际口语正文。"""
    voice = Mock(synthesize=Mock(return_value=SynthesisStream(["frame"])))
    agent = NodeAgent(voices={"neutral": voice})
    assert [frame async for frame in agent.tts_node(source("😊！\n你好。]"), ModelSettings())] == [
        "frame"
    ]
    voice.synthesize.assert_called_once()
    assert voice.synthesize.call_args.args[0] == "你好。"


async def test_agent_does_not_repeat_service_fallback_before_or_after_audio():
    """服务已拥有回退预算，Agent 在首帧前和发声后都不能再重放同一句。"""
    failed = SynthesisStream([RuntimeError("offline")])
    natural = SynthesisStream(["natural-frame"])
    voices = {
        "happy": Mock(synthesize=Mock(return_value=failed)),
        "neutral": Mock(synthesize=Mock(return_value=natural)),
    }
    agent = NodeAgent(voices=voices, style="happy")
    with pytest.raises(RuntimeError, match="offline"):
        _ = [frame async for frame in agent._synthesize(SpeechSegment("你好。"))]
    assert failed.closed and not natural.closed
    assert voices["happy"].synthesize.call_args.kwargs["conn_options"].max_retry == 0
    voices["neutral"].synthesize.assert_not_called()
    voices["neutral"].synthesize.reset_mock()
    voices["happy"].synthesize.return_value = SynthesisStream(["frame", RuntimeError("mid-stream")])
    frames = []
    with pytest.raises(RuntimeError, match="mid-stream"):
        async for frame in agent._synthesize(SpeechSegment("你好。")):
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
            async for frame in NodeAgent(voices=voices, style="concerned")._synthesize(
                SpeechSegment("没关系。")
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


async def test_shared_output_installs_once_and_never_owns_snapshot(monkeypatch):
    """只有会话可登记与关闭快照，重复启用和关闭后的迟到回调不重建媒体资源。"""
    from xiaoya.infrastructure import expressive_agent

    agent = NodeAgent()
    original_audio = Mock()
    agent.test_session.output.audio = original_audio
    endpoint = Mock(registered=True, instance="shared-job", aclose=AsyncMock())
    publisher = Mock(aclose=AsyncMock())
    synchronizer = Mock(audio_output=Mock(), text_output=Mock(), aclose=AsyncMock())
    output_factory = Mock(return_value=publisher)
    sync_factory = Mock(return_value=synchronizer)
    monkeypatch.setattr(expressive_agent, "DeliveryTextOutput", output_factory)
    monkeypatch.setattr(expressive_agent, "TranscriptSynchronizer", sync_factory)
    await agent.enable_delivery(endpoint)
    await agent.enable_delivery(endpoint)
    output_factory.assert_called_once_with(
        agent._room, snapshot_endpoint=endpoint, presentation=agent._presentation
    )
    sync_factory.assert_called_once_with(
        next_in_chain_audio=original_audio, next_in_chain_text=publisher
    )
    await agent.aclose()
    await agent.enable_delivery(endpoint)
    output_factory.assert_called_once()
    publisher.aclose.assert_awaited_once()
    synchronizer.aclose.assert_awaited_once()
    endpoint.aclose.assert_not_awaited()


async def test_output_rejects_closed_snapshot_before_allocating(monkeypatch):
    """已撤销的授权端点不能创建新的表现输出，避免注销后重新获得播放许可。"""
    from xiaoya.infrastructure import expressive_agent

    factory = Mock()
    monkeypatch.setattr(expressive_agent, "DeliveryTextOutput", factory)
    with pytest.raises(RuntimeError, match="已关闭"):
        await NodeAgent().enable_delivery(Mock(registered=False))
    factory.assert_not_called()


async def test_enabled_adapter_uses_immutable_private_voice_presets(
    monkeypatch, private_settings: Settings
):
    """开启表现不引入第二语音出口，所有有限预设继续使用同一私有音色。"""
    monkeypatch.setattr(
        livekit_conversation,
        "ConversationConnection",
        Mock(return_value=Mock(bind=AsyncMock(), aclose=AsyncMock())),
    )
    session = Mock(start=AsyncMock(), say=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(livekit_conversation, "AgentSession", Mock(return_value=session))
    monkeypatch.setattr(livekit_conversation.inference, "TurnDetector", Mock())
    monkeypatch.setattr(
        livekit_conversation, "LocalStreamingSTT", Mock(return_value=Mock(aclose=AsyncMock()))
    )
    monkeypatch.setattr(
        livekit_conversation.openai, "LLM", Mock(return_value=Mock(aclose=AsyncMock()))
    )
    factory = Mock(side_effect=lambda **kwargs: Mock(aclose=AsyncMock()))
    monkeypatch.setattr(livekit_conversation.openai, "TTS", factory)
    adapter = livekit_conversation.LiveKitVoiceConversation(
        room=Mock(isconnected=Mock(return_value=False)),
        settings=private_settings,
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
    session.say.assert_awaited_once_with("你好。")
    await adapter.close()
    session.aclose.assert_awaited_once()


async def test_markdown_is_filtered_only_at_audio_boundary():
    """正文不承载协议，朗读边界仍使用 SDK 过滤 Markdown 和 emoji。"""
    raw = "(Hello) **Good morning**. 😀"
    voice = Mock(synthesize=Mock(return_value=SynthesisStream(["frame"])))
    agent = NodeAgent(voices={"happy": voice}, style="happy")
    assert [frame async for frame in agent.tts_node(source(raw), ModelSettings())] == ["frame"]
    body = voice.synthesize.call_args.args[0]
    assert "Hello" in body and "Good morning" in body
    assert "happy" not in body and "|" not in body and "**" not in body and "😀" not in body


async def test_identical_precomputed_text_keeps_committed_voice_order_for_transcription():
    """下一段同字正文可提前合成；字幕稍后绑定时必须按真实 HTTP 顺序消费冻结声音。"""
    happy = Mock(synthesize=Mock(return_value=SynthesisStream(["happy-frame"])))
    gentle = Mock(synthesize=Mock(return_value=SynthesisStream(["gentle-frame"])))
    agent = NodeAgent(voices={"happy": happy, "gentle": gentle}, style="happy")
    text = "你好呀。"
    turn = agent._presentation.turn_id
    assert [frame async for frame in agent._synthesize(SpeechSegment(text))] == ["happy-frame"]
    agent._presentation.voice_for.return_value = DeliveryIntent("gentle")
    assert [frame async for frame in agent._synthesize(SpeechSegment(text))] == ["gentle-frame"]
    assert list(agent._voice_intents[(turn, text)]) == [
        DeliveryIntent("happy"),
        DeliveryIntent("gentle"),
    ]
    handle = Mock(id="reply", interrupted=False, done=Mock(return_value=False))
    agent.test_session.current_speech = handle
    agent._delivery = Mock(bind_segment=AsyncMock(), cancel_reply=AsyncMock(), aclose=AsyncMock())
    for _ in range(2):
        assert [body async for body in agent.transcription_node(source(text), ModelSettings())] == [
            text
        ]
    bindings = agent._delivery.bind_segment.await_args_list
    assert [call.args for call in bindings] == [
        ("reply", DeliveryIntent("happy"), text),
        ("reply", DeliveryIntent("gentle"), text),
    ]
    assert (turn, text) not in agent._voice_intents
    assert agent._committed_voice(text) == DeliveryIntent()
    happy.synthesize.assert_called_once()
    gentle.synthesize.assert_called_once()
    assert happy.synthesize.call_args.args[0] == gentle.synthesize.call_args.args[0] == text
    assert agent._committed_voice("其他正文。") == DeliveryIntent()
    await agent.aclose()
