"""单片段公开转写与SDK累计提交使用同一来源，不能以放松出生门换取出声。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from livekit.agents import AgentSession, ModelSettings, StopResponse, llm, stt
from livekit.agents.voice.agent_activity import AgentActivity
from livekit.agents.voice.audio_recognition import AudioRecognition
from livekit.agents.voice.events import UserInputTranscribedEvent
from livekit.agents.voice.speech_handle import SpeechHandle
from test_delivery_output import delivery as delivery
from test_expressive_agent import source
from test_speech_origin_turn import chain as chain

from xiaoya.infrastructure.livekit_conversation import LiveKitVoiceConversation, TextInputEvent


@pytest.fixture
def conversation(chain):
    """仅隔离外部判断；正式公开事件、应用控制器和音频许可共同暴露片段身份分叉。"""
    agent, *_ = chain
    agent._presentation._decider = SimpleNamespace(
        decide_user=AsyncMock(return_value=None), aclose=AsyncMock()
    )
    result = object.__new__(LiveKitVoiceConversation)
    result._closed = False
    result._presentation = agent._presentation
    result._expressive_agent = agent
    return result


def transcribe(conversation, text, *, final=True, at=100.0):
    """保留SDK事件的原文与公开时刻，避免把完整累计输入伪装成单条STT片段。"""
    conversation._on_transcription(
        UserInputTranscribedEvent(transcript=text, is_final=final, created_at=at)
    )


def message(text, *, final_at=None):
    """SDK将最后final时刻编码为停声加转写延迟；缺失时明确保持缺失而非补假身份。"""
    result = llm.ChatMessage(role="user", content=[text])
    if final_at is not None:
        result.metrics = {"stopped_speaking_at": final_at, "transcription_delay": 0.0}
    return result


async def audible_reply(agent, audio, gate, text="太好了。"):
    """验证到真实生产字幕及音频下游，不能只用内部布尔值代表合法handle已经能出声。"""
    assert [x async for x in agent.transcription_node(source(text), ModelSettings())] == [text]
    await gate.capture_frame(
        rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=240)
    )
    assert len(audio.frames) == 1


@pytest.mark.parametrize("chunks", [("考试通过了。",), ("考试通过了。", "特别开心。")])
async def test_sdk_cumulative_commit_keeps_latest_birth_and_real_audio(chain, conversation, chunks):
    """复现真实两final后提交：最新累计全文预生成必须原位晋升，旧片段出生仍严格拒绝。"""
    agent, _, _, audio, gate = chain
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    aggregate = ""
    handles = []
    for index, chunk in enumerate(chunks):
        transcribe(conversation, chunk, at=100.0 + index)
        aggregate = (aggregate + " " + chunk).lstrip()
        handle = SpeechHandle.create()
        agent.register_speech(handle)
        agent.test_session.current_speech = handle
        handles.append(handle)
        assert agent._presentation._user_text == aggregate
    turn, revision = agent._presentation.turn_id, agent._speech_input_revision
    conversation._on_user_state(SimpleNamespace(new_state="listening"))
    await agent.on_user_turn_completed(llm.ChatContext(), message(aggregate, final_at=102.0))
    assert agent._presentation.turn_id == turn and agent._speech_input_revision == revision
    assert agent._can_transcribe(handles[-1])
    assert all(not agent._can_transcribe(old) for old in handles[:-1])
    await audible_reply(agent, audio, gate)


async def test_interim_is_temporary_and_same_turn_vad_resume_keeps_final_prefix(
    chain, conversation
):
    """SDK暂停不清final；两个interim只能替换临时尾部，最后final才追加到稳定前缀。"""
    agent, *_ = chain
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "第一段。")
    turn = agent._presentation.turn_id
    conversation._on_user_state(SimpleNamespace(new_state="listening"))
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "第二", final=False, at=101.0)
    assert agent._presentation._user_text == "第一段。 第二"
    transcribe(conversation, "第二段", final=False, at=102.0)
    assert agent._presentation._user_text == "第一段。 第二段"
    transcribe(conversation, "第二段。", at=103.0)
    assert agent._presentation.turn_id == turn
    assert agent._presentation._user_text == "第一段。 第二段。"
    await agent.on_user_turn_completed(
        llm.ChatContext(), message("第一段。 第二段。", final_at=104.0)
    )


async def test_repeated_raw_final_is_sdk_append_not_text_dedup(chain, conversation):
    """SDK重复final仍逐片追加，不能以文本去重改变权威提交或为旧出生续权。"""
    agent, *_ = chain
    transcribe(conversation, "你好")
    first = SpeechHandle.create()
    agent.register_speech(first)
    transcribe(conversation, "你好", at=101.0)
    assert agent._presentation._user_text == "你好 你好"
    second = SpeechHandle.create()
    agent.register_speech(second)
    agent.test_session.current_speech = second
    await agent.on_user_turn_completed(llm.ChatContext(), message("你好 你好", final_at=102.0))
    assert agent._can_transcribe(second) and not agent._can_transcribe(first)


async def test_cumulative_text_uses_exact_sdk_lstrip_not_whitespace_normalization(
    chain, conversation
):
    """只复现SDK的一个空格拼接和lstrip，不能替换内部空格、标点或片段尾部字符。"""
    agent, *_ = chain
    transcribe(conversation, "  第一段  ")
    transcribe(conversation, " 第二段。 ", at=101.0)
    assert agent._speech_input[1] == "第一段    第二段。"
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    await agent.on_user_turn_completed(
        llm.ChatContext(), message("第一段    第二段。 ", final_at=102.0)
    )
    assert agent._can_transcribe(handle)


@pytest.mark.parametrize("same_text", [False, True])
async def test_delayed_old_hook_cannot_mutate_new_input_then_valid_hook_recovers(
    chain, conversation, same_text
):
    """旧EOU可等待到新final后才执行；公开时钟拒同字旧提交，拒绝不能清新缓存或撤新出生。"""
    agent, _, _, audio, gate = chain
    transcribe(conversation, "旧请求", at=100.0)
    agent._presentation.phase("thinking")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    current = "旧请求" if same_text else "新请求"
    transcribe(conversation, current, at=200.0)
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    before = (agent._presentation.turn_id, agent._speech_input_revision, agent._speech_input)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("旧请求", final_at=101.0))
    assert before == (
        agent._presentation.turn_id,
        agent._speech_input_revision,
        agent._speech_input,
    )
    assert agent._can_transcribe(handle)
    await agent.on_user_turn_completed(llm.ChatContext(), message(current, final_at=201.0))
    assert agent._can_transcribe(handle)
    await audible_reply(agent, audio, gate, "回应当前请求。")


async def test_ambiguous_old_hook_without_metrics_is_rejected_without_poisoning_new_turn(
    chain, conversation
):
    """无公共EOU身份且同文重叠时必须拒绝；不能用猜测默认它属于最新输入。"""
    agent, *_ = chain
    transcribe(conversation, "同一句", at=100.0)
    agent._presentation.phase("thinking")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "同一句", at=200.0)
    before = (agent._presentation.turn_id, agent._speech_input_revision)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("同一句"))
    assert before == (agent._presentation.turn_id, agent._speech_input_revision)
    await agent.on_user_turn_completed(llm.ChatContext(), message("同一句", final_at=201.0))


async def test_mismatched_hook_does_not_guess_suffix_and_later_matching_hook_is_usable(
    chain, conversation
):
    """公开API不报告accepted EOU，缺边界时保守拒绝，不截取suffix猜新轮且不污染后续有效提交。"""
    agent, *_ = chain
    transcribe(conversation, "A", at=100.0)
    transcribe(conversation, "B", at=101.0)
    before = (agent._presentation.turn_id, agent._speech_input_revision, agent._speech_input)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("B", final_at=102.0))
    assert before == (
        agent._presentation.turn_id,
        agent._speech_input_revision,
        agent._speech_input,
    )
    await agent.on_user_turn_completed(llm.ChatContext(), message("A B", final_at=102.0))


async def test_pending_jev_is_not_a_commit_or_audio_dependency(chain, conversation):
    """累计提交依旧不等Jev，挂起完整预算不能导致出生换轮或把语义等待加进首声。"""
    agent, _, _, audio, gate = chain
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(context):
        """只暂停外部语义端口，测试音频链自身不等待此任务。"""
        entered.set()
        await release.wait()
        return None

    agent._presentation._decider.decide_user = pending
    transcribe(conversation, "第一段")
    transcribe(conversation, "第二段", at=101.0)
    await entered.wait()
    task = agent._presentation._user_task
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    try:
        await agent.on_user_turn_completed(
            llm.ChatContext(), message("第一段 第二段", final_at=102.0)
        )
        assert agent._presentation._user_task is task and not task.done()
        await audible_reply(agent, audio, gate)
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.parametrize("boundary", ["typed", "empty", "close"])
async def test_explicit_boundary_drops_old_prefix_without_reanimating_birth(
    chain, conversation, boundary
):
    """文字、显式空final和关闭都撤旧语音前缀；旧完成回调不得重新提交或让会话复活。"""
    agent, *_ = chain
    transcribe(conversation, "旧段", at=100.0)
    old = SpeechHandle.create()
    agent.register_speech(old)
    agent.test_session.current_speech = old
    if boundary == "typed":
        agent.complete_user_turn("文字输入")
    elif boundary == "empty":
        transcribe(conversation, "", at=101.0)
    else:
        await agent.aclose()
    revision = agent._speech_input_revision
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("旧段", final_at=102.0))
    assert agent._speech_input_revision == revision and not agent._can_transcribe(old)
    if boundary != "close":
        agent._presentation.phase("thinking")
        conversation._on_user_state(SimpleNamespace(new_state="speaking"))
        transcribe(conversation, "新段", at=200.0)
        assert agent._presentation._user_text == "新段"


async def test_no_vad_idle_new_input_cannot_keep_abandoned_prefix_or_send_mixed_query(
    chain, conversation
):
    """SDK无VAD也能从idle建立轮次；同步修正前缀不能让中间混合正文发出外部查询。"""
    agent, *_ = chain
    transcribe(conversation, "旧段", at=100.0)
    await agent._presentation._user_task
    decider = agent._presentation._decider.decide_user
    decider.reset_mock()
    origin = agent._presentation.turn_id
    agent._presentation.phase("idle")
    transcribe(conversation, "新段", at=200.0)
    await agent._presentation._user_task
    assert agent._presentation.turn_id != origin
    assert agent._presentation._user_text == "新段"
    assert [call.args[0].user_text for call in decider.call_args_list] == ["新段"]
    await agent.on_user_turn_completed(llm.ChatContext(), message("新段", final_at=201.0))


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0])
async def test_ambiguous_input_cannot_use_invalid_public_metrics_as_identity(
    chain, conversation, invalid
):
    """缺失与无效公共计时都不能替同文重叠提供身份，且不能因此修改当前输入。"""
    agent, *_ = chain
    transcribe(conversation, "重复", at=100.0)
    agent._presentation.phase("thinking")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "重复", at=200.0)
    current = message("重复", final_at=201.0)
    current.metrics["transcription_delay"] = invalid
    before = (agent._presentation.turn_id, agent._speech_input_revision, agent._speech_input)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), current)
    assert before == (
        agent._presentation.turn_id,
        agent._speech_input_revision,
        agent._speech_input,
    )


async def test_wall_clock_rollback_cannot_authorize_same_text_from_old_eou(chain, conversation):
    """WSL墙钟实际会回拨，旧EOU数字较新不代表当前输入，不能据此提交新轮同文。"""
    agent, *_ = chain
    transcribe(conversation, "重复", at=100.0)
    agent._presentation.phase("thinking")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "重复", at=94.0)
    before = (agent._presentation.turn_id, agent._speech_input_revision, agent._speech_input)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("重复", final_at=101.0))
    assert before == (
        agent._presentation.turn_id,
        agent._speech_input_revision,
        agent._speech_input,
    )


async def test_typed_boundary_also_clears_real_sdk_accumulator(chain, conversation):
    """仅清本地缓存会与SDK未提交前缀分叉，真实公开clear链必须在文字生成前清两端。"""
    agent, *_ = chain
    session = AgentSession(turn_detection="manual")
    hooks = Mock()
    recognition = AudioRecognition(
        session,
        hooks=hooks,
        endpointing=Mock(),
        stt=None,
        vad=None,
        interruption_detection=None,
        turn_detection="manual",
    )
    activity = object.__new__(AgentActivity)
    activity._audio_recognition = recognition
    activity._rt_session = None
    session._activity = activity
    session.interrupt = AsyncMock()
    session.generate_reply = Mock()
    conversation._session = session
    conversation._started = True
    conversation._text_input_lock = asyncio.Lock()
    agent.set_environment(replace(agent.environment, channel="console"))

    def public_final(event, **kwargs):
        """SDK仍亲自累计，仅将其真实单片段公开回调接到生产会话适配器。"""
        transcribe(conversation, event.alternatives[0].text)

    def final(text):
        """通过已安装SDK实际STT处理路径建立前缀，避免测试只伪造本地缓存。"""
        recognition._process_stt_event(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language="zh", text=text)],
            )
        )

    hooks.on_final_transcript.side_effect = public_final
    final("尚未结束的A")
    assert recognition._audio_transcript == agent._asr_text == "尚未结束的A"
    await conversation._on_text_input(session, TextInputEvent(text="文字请求", participant=None))
    assert recognition._audio_transcript == ""
    session.generate_reply.assert_called_once_with(user_input="文字请求")
    agent._presentation.phase("thinking")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    final("下一轮B")
    assert recognition._audio_transcript == agent._asr_text == "下一轮B"


async def test_two_revoked_same_text_hooks_are_consumed_before_current_without_new_birth(
    chain, conversation
):
    """串行旧hook各只消费自己的同文墓碑，不能改当前轮次或让当前handle重新登记。"""
    agent, _, _, audio, gate = chain
    for _ in range(2):
        transcribe(conversation, "同一句")
        agent._presentation.phase("thinking")
        conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "同一句")
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    before = (agent._presentation.turn_id, agent._speech_input_revision, agent._speech_input)
    for remaining in [1, 0]:
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(llm.ChatContext(), message("同一句"))
        assert list(agent._asr_revoked).count("同一句") == remaining
        assert before == (
            agent._presentation.turn_id,
            agent._speech_input_revision,
            agent._speech_input,
        )
        assert agent._can_transcribe(handle)
    await agent.on_user_turn_completed(llm.ChatContext(), message("同一句"))
    assert agent._can_transcribe(handle)
    await audible_reply(agent, audio, gate)


async def test_every_observed_final_prefix_is_revoked_not_only_the_last(chain, conversation):
    """旧EOU可能只对应较早final，撤销目录必须包含观察到的每个精确前缀而非猜suffix。"""
    agent, *_ = chain
    transcribe(conversation, "A")
    transcribe(conversation, "B")
    agent._presentation.phase("thinking")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "A")
    assert list(agent._asr_revoked) == ["A", "A B"]
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("A"))
    assert list(agent._asr_revoked) == ["A B"]
    await agent.on_user_turn_completed(llm.ChatContext(), message("A"))
    assert not agent._asr_revoked


async def test_uncommitted_tombstone_conservatively_drops_once_then_clean_input_can_recover(
    chain, conversation
):
    """旧final未真正EOU无法由公开API区分，会保守丢同文一次；下一可归属新输入不应永久哑。"""
    agent, *_ = chain
    transcribe(conversation, "尚未EOU")
    agent.complete_user_turn("文字替代")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "尚未EOU")
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message("尚未EOU"))
    assert not agent._asr_revoked
    agent._presentation.phase("idle")
    conversation._on_user_state(SimpleNamespace(new_state="speaking"))
    transcribe(conversation, "明确的新问题")
    await agent.on_user_turn_completed(llm.ChatContext(), message("明确的新问题"))
    assert not agent._asr_revoked and not agent._asr_overflow


async def test_identity_capacity_never_evicts_old_input_to_reopen_permission(
    chain, conversation, caplog
):
    """有限目录满后须明确重连，不允许淘汰旧身份使同文迟到hook重新获得授权。"""
    agent, *_ = chain
    for _ in range(65):
        transcribe(conversation, "片段")
    assert agent._asr_overflow
    assert len(agent._asr_candidates) + len(agent._asr_revoked) == 64
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), message(agent._asr_text))
    assert "语音输入身份目录已满，需重新连接会话" in caplog.text
    await agent.aclose()
    assert not agent._asr_candidates and not agent._asr_revoked and not agent._asr_overflow
