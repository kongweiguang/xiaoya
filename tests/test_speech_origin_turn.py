"""公开出生事件固定回复原轮次，SDK尚未换handle不能替旧音频续发许可。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from livekit.agents import ModelSettings, llm
from livekit.agents.voice.speech_handle import SpeechHandle
from test_delivery_output import MemoryAudioOutput
from test_delivery_output import delivery as delivery
from test_expressive_agent import NodeAgent, source

from xiaoya.infrastructure.expressive_agent import _BoundAudioOutput
from xiaoya.infrastructure.livekit_conversation import LiveKitVoiceConversation


@pytest.fixture
async def chain(delivery):
    """只替换房间和设备，控制器、字幕输出、handle和音频许可仍使用生产实现。"""
    output, participant, _ = delivery
    agent = NodeAgent()
    agent._presentation = output._presentation
    agent._delivery = output
    audio = MemoryAudioOutput()
    gate = agent._audio_binding = _BoundAudioOutput(audio, agent._audio_owner)
    try:
        yield agent, output, participant, audio, gate
    finally:
        await agent.aclose()
        await agent._presentation.aclose()


async def test_missing_birth_never_lazily_adopts_current_turn(chain):
    """缺出生事实必须关门；首次字幕回调不能把迟到旧handle登记为当前新轮。"""
    agent, output, participant, _, _ = chain
    handle = SpeechHandle.create()
    agent.test_session.current_speech = handle
    agent._presentation.complete_turn("新输入")
    assert not agent._can_transcribe(handle)
    assert agent._audio_owner() == ""
    assert [
        x async for x in agent.transcription_node(source("迟到旧正文。"), ModelSettings())
    ] == []
    await output.drain()
    assert participant.streams == [] and output._playback_binding is None


async def test_new_turn_denies_old_handle_still_current_in_sdk(chain):
    """复现真实分叉：应用已经换轮，SDK旧handle尚未interrupted或done，不能继续授字幕。"""
    agent, output, _, _, _ = chain
    agent._presentation.complete_turn("挥挥手")
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    assert await agent._bind(handle, "好，")
    origin = agent._presentation.turn_id
    agent._presentation.user_speaking(True)
    assert agent._presentation.turn_id != origin
    assert agent.session.current_speech is handle and not handle.interrupted and not handle.done()
    assert output._playback_binding is None
    assert not agent._can_transcribe(handle) and agent._audio_owner() == ""
    assert not await agent._bind(handle, "迟到后续。")
    assert output._playback_binding is None


async def test_already_released_audio_cannot_capture_after_origin_turn_changes(chain):
    """第一帧曾有效也不能授权整条流；新VAD轮次后下一帧须在真实下游前拒绝。"""
    agent, _, _, audio, gate = chain
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    permit = gate.begin_text(handle.id)
    gate.release(permit, allowed=True)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=240)
    await gate.capture_frame(frame)
    agent._presentation.user_speaking(True)
    with pytest.raises(asyncio.CancelledError):
        await gate.capture_frame(frame)
    assert len(audio.frames) == 1


async def test_turn_change_between_binding_and_first_frame_cannot_release_audio(chain):
    """字幕已绑定但首帧未到的实际窗口，也必须以出生轮次重核而非沿用旧布尔值。"""
    agent, _, _, audio, gate = chain
    agent._presentation.complete_turn("挥挥手")
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    permit = gate.begin_text(handle.id)
    assert await agent._bind(handle, "好，")
    agent._presentation.user_speaking(True)
    gate.release(permit, allowed=agent._can_transcribe(handle))
    with pytest.raises(asyncio.CancelledError):
        await gate.capture_frame(
            rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=240)
        )
    assert audio.frames == []


async def test_duplicate_birth_cannot_rebase_old_handle_to_new_turn(chain):
    """重复公开事件幂等，不能用重复登记把原始旧轮次改写成新轮。"""
    agent, _, _, _, _ = chain
    agent._presentation.complete_turn("旧输入")
    handle = Mock(id="old", done=Mock(return_value=False), interrupted=False)
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    origin = agent._presentation.turn_id
    agent._presentation.user_speaking(True)
    agent.register_speech(handle)
    assert agent._presentation.turn_id != origin and not agent._can_transcribe(handle)
    handle.add_done_callback.assert_called_once_with(agent._speech_done)


async def test_precommit_handle_keeps_same_final_asr_turn_on_commit(chain):
    """预生成出生可早于commit；相同最终ASR原位晋升不能误杀真实合法的预生成回复。"""
    agent, _, _, _, _ = chain
    controller = agent._presentation
    controller._decider = SimpleNamespace(
        decide_user=AsyncMock(return_value=None), aclose=AsyncMock()
    )
    conversation = object.__new__(LiveKitVoiceConversation)
    conversation._closed = False
    conversation._presentation = controller
    conversation._expressive_agent = agent
    controller.user_speaking(True)
    conversation._on_transcription(SimpleNamespace(transcript="你好", is_final=True))
    await controller._user_task
    turn = controller.turn_id
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    controller.user_speaking(False)
    controller.phase("thinking")
    agent.complete_user_turn("你好")
    assert controller.turn_id == turn and agent._can_transcribe(handle)
    assert [x async for x in agent.transcription_node(source("你好呀。"), ModelSettings())] == [
        "你好呀。"
    ]


async def test_pending_jev_does_not_rebase_same_final_commit_or_block_text(chain):
    """complete标志指完整查询而非已完成；Jev在途时同文提交仍保留原出生并立即转发正文。"""
    agent, _, _, _, _ = chain
    controller = agent._presentation
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending_decision(context):
        """真实后台任务停在外部判断await，提交与正文不得依赖它先完成。"""
        entered.set()
        await release.wait()
        return None

    controller._decider = SimpleNamespace(decide_user=pending_decision, aclose=AsyncMock())
    conversation = object.__new__(LiveKitVoiceConversation)
    conversation._closed = False
    conversation._presentation = controller
    conversation._expressive_agent = agent
    controller.user_speaking(True)
    conversation._on_transcription(SimpleNamespace(transcript="你好", is_final=True))
    await entered.wait()
    task, origin = controller._user_task, controller.turn_id
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    try:
        controller.user_speaking(False)
        controller.phase("thinking")
        await agent.on_user_turn_completed(
            llm.ChatContext(), llm.ChatMessage(role="user", content=["你好"])
        )
        assert controller.turn_id == origin and controller._user_task is task
        assert not task.done() and not controller._user_result_ready
        assert agent._can_transcribe(handle)
        assert [x async for x in agent.transcription_node(source("你好呀。"), ModelSettings())] == [
            "你好呀。"
        ]
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.parametrize("text,final", [("改成摇头", True), ("挥挥手", False), ("", True)])
async def test_changed_or_resumed_asr_rejects_precommit_origin_in_same_turn(chain, text, final):
    """同一累计正文观察幂等，改文或继续讲话撤旧出生；重复原始final由SDK追加，另例覆盖。"""
    agent, _, _, _, _ = chain
    controller = agent._presentation
    controller._decider = SimpleNamespace(
        decide_user=AsyncMock(return_value=None), aclose=AsyncMock()
    )
    conversation = object.__new__(LiveKitVoiceConversation)
    conversation._closed = False
    conversation._presentation = controller
    conversation._expressive_agent = agent
    controller.user_speaking(True)
    conversation._on_transcription(SimpleNamespace(transcript="挥挥手", is_final=True))
    await controller._user_task
    turn = controller.turn_id
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    agent.observe_user_transcription("挥挥手", final=True)
    assert agent._can_transcribe(handle)
    conversation._on_transcription(SimpleNamespace(transcript=text, is_final=final))
    assert controller.turn_id == turn
    assert not agent._can_transcribe(handle) and agent._audio_owner() == ""
    agent.register_speech(handle)
    assert not agent._can_transcribe(handle)
    replacement = SpeechHandle.create()
    agent.register_speech(replacement)
    agent.test_session.current_speech = replacement
    assert agent._can_transcribe(replacement)


async def test_typed_commit_sets_birth_basis_and_same_text_new_turn_stays_independent(chain):
    """文字入口没有ASR事件，必须先更新来源；同字新提交仍有独立轮次，不能复用旧出生。"""
    agent, _, _, _, _ = chain
    agent.complete_user_turn("你好")
    first = SpeechHandle.create()
    agent.register_speech(first)
    agent.test_session.current_speech = first
    origin = agent._presentation.turn_id
    assert agent._can_transcribe(first)
    agent.complete_user_turn("你好")
    assert agent._presentation.turn_id != origin and not agent._can_transcribe(first)
    second = SpeechHandle.create()
    agent.register_speech(second)
    agent.test_session.current_speech = second
    assert agent._can_transcribe(second)


async def test_new_generation_works_and_old_finally_cannot_touch_new_permit(chain):
    """旧token收尾继续只作用于旧段，新增原轮次检查不能破坏原有跨handle隔离。"""
    agent, _, _, audio, gate = chain
    old = SpeechHandle.create()
    agent.register_speech(old)
    agent.test_session.current_speech = old
    old_permit = gate.begin_text(old.id)
    leave = asyncio.Event()

    async def old_finally():
        """保持SDK音频forward的同任务finally形状，让旧ContextVar真实跨越新handle。"""
        await leave.wait()
        gate.flush()
        gate.clear_buffer()

    task = asyncio.create_task(old_finally())
    agent._presentation.user_speaking(True)
    new = SpeechHandle.create()
    agent.register_speech(new)
    agent.test_session.current_speech = new
    current = gate.begin_text(new.id)
    leave.set()
    await task
    assert not current.audio_flushed and not current.ready.is_set()
    gate.release(old_permit, allowed=True)
    gate.release(current, allowed=agent._can_transcribe(new))
    await gate.capture_frame(
        rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=240)
    )
    assert len(audio.frames) == 1


async def test_console_and_say_have_origin_without_delivery(chain):
    """没有舞台的console也走公开出生事实，不依赖网页装配才能正常输出净正文。"""
    agent, _, _, _, _ = chain
    agent._delivery = None
    agent._audio_binding.close()
    agent._audio_binding = None
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    assert [x async for x in agent.transcription_node(source("开场你好。"), ModelSettings())] == [
        "开场你好。"
    ]


async def test_zero_frame_done_and_close_release_origin_records(chain):
    """出生即登记回收，零帧或没进入字幕节点的handle也不能依赖进程结束清映射。"""
    agent, _, _, _, _ = chain
    handle = Mock(id="empty", done=Mock(return_value=False), interrupted=False)
    handle.exception.return_value = None
    agent.register_speech(handle)
    assert handle in agent._speech_origins
    handle.done.return_value = True
    agent._speech_done(handle)
    await asyncio.gather(*agent._tasks)
    assert handle not in agent._speech_origins
    late = SpeechHandle.create()
    agent.register_speech(late)
    await agent.aclose()
    assert not agent._speech_origins and agent._speech_input is None
    agent.register_speech(late)
    agent.observe_user_transcription("关闭后晚到原文", final=True)
    assert not agent._speech_origins
    assert agent._speech_input is None


async def test_public_birth_event_is_wired_and_closed_conversation_cannot_register(chain):
    """装配只转交公开SpeechCreatedEvent；关闭后的迟到事件不能让旧会话重新拥有handle。"""
    agent, _, _, _, _ = chain
    conversation = object.__new__(LiveKitVoiceConversation)
    conversation._closed = False
    conversation._expressive_agent = agent
    conversation._session = Mock()
    conversation._subscriptions = []
    conversation._bind_session_events()
    handlers = {call.args[0]: call.args[1] for call in conversation._session.on.call_args_list}
    handle = SpeechHandle.create()
    handlers["speech_created"](SimpleNamespace(speech_handle=handle))
    agent.test_session.current_speech = handle
    assert agent._can_transcribe(handle)
    conversation._closed = True
    late = SpeechHandle.create()
    handlers["speech_created"](SimpleNamespace(speech_handle=late))
    assert late not in agent._speech_origins
