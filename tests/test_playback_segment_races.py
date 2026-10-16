"""正式同步器的并发字幕/音频路径，防止只测顺序调用掩盖句段轮换缺口。"""

import asyncio
import json
import time

import pytest
from livekit import rtc
from livekit.agents import ModelSettings
from livekit.agents.voice.generation import (
    forward_generation,
    perform_audio_forwarding,
    perform_text_forwarding,
)
from livekit.agents.voice.speech_handle import SpeechHandle
from livekit.agents.voice.transcription import TranscriptSynchronizer
from test_delivery_output import MemoryAudioOutput
from test_delivery_output import delivery as delivery
from test_expressive_agent import NodeAgent

from xiaoya.infrastructure.delivery_output import DELIVERY_TOPIC
from xiaoya.infrastructure.expressive_agent import _BoundAudioOutput


class ScheduledAudioOutput(MemoryAudioOutput):
    """保留公开播放事件，只压缩设备时间，不绕过正式同步器和转发协程。"""

    def __init__(self) -> None:
        """每段只在flush后报告播放完成，模拟房间音频输出的独立调度。"""
        super().__init__()
        self.started = False
        self.duration = 0.0
        self.finished = asyncio.Event()
        self.clears = 0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """真实链的首帧事件与字幕任务竞争，不人为保证字幕先运行。"""
        await super().capture_frame(frame)
        self.duration += frame.duration
        if not self.started:
            self.started = True
            self.on_playback_started(created_at=time.time())
        await asyncio.sleep(0)

    def flush(self) -> None:
        """异步收尾使下一段可能在旧同步器清理期间进入。"""
        super().flush()
        if self.started:
            asyncio.get_running_loop().call_later(0.01, self._finish)

    def _finish(self) -> None:
        """先收回设备状态再公开事件，不能把下一段的样本算到旧段。"""
        duration, self.duration = self.duration, 0.0
        self.started = False
        self.on_playback_finished(playback_position=duration, interrupted=False)
        self.finished.set()

    def clear_buffer(self) -> None:
        """内存设备也记录清队列调用，空许可表不能掩盖迟到旧取消误清新播放。"""
        self.clears += 1
        super().clear_buffer()


async def delayed_text(text: str, delay: float):
    """模拟字幕入口调度稍晚，不改变原正文或利用固定动作标签。"""
    await asyncio.sleep(delay)
    yield text


async def buffered_audio():
    """预合成音频可以立即入队；字幕不能依赖网络恰好更快。"""
    yield rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)


async def empty_audio():
    """首帧前失败仍有SDK flush，不能把该段迟到字幕绑定给下一段正常音频。"""
    if False:
        yield


@pytest.mark.parametrize("text_delay", [0, 0.001, 0.02])
async def test_three_sdk_segments_keep_exact_transcripts_when_audio_runs_first(
    delivery, text_delay: float, caplog
):
    """同一handle三段必须各自完整开流，禁止用下一句身份补足中间已经播放的音频。

    显式模拟公开出生事件，不能靠字幕首次调用懒登记掩盖原轮次缺失。
    """
    output, participant, _ = delivery
    audio = ScheduledAudioOutput()
    synchronizer = TranscriptSynchronizer(next_in_chain_audio=audio, next_in_chain_text=output)
    agent = NodeAgent()
    agent._delivery = output
    agent._synchronizer = synchronizer
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    agent._audio_binding = _BoundAudioOutput(synchronizer.audio_output, agent._audio_owner)
    texts = [
        "好呀，那我先来问几个。",
        "你平时喜欢安静的活动，还是更喜欢和朋友一起动起来？",
        "比如运动、散步，或者看书、听歌、打游戏这类。",
    ]
    try:
        for text in texts:
            await forward_generation(
                speech_handle=handle,
                audio_output=agent._audio_binding,
                text_output=synchronizer.text_output,
                audio_source=buffered_audio(),
                text_source=agent.transcription_node(
                    delayed_text(text, text_delay), ModelSettings()
                ),
                on_first_frame=lambda *_: None,
                reconcile_playout_pause=lambda: None,
            )
        await synchronizer.barrier()
        await output.drain()
        assert ["".join(writer.chunks) for _, writer in participant.streams] == texts
        headers = [json.loads(attributes[DELIVERY_TOPIC]) for attributes, _ in participant.streams]
        assert len({header["segment_id"] for header in headers}) == 3
        assert all(header["state"] == "active" for header in headers)
        assert not any("marked as ended" in record.message for record in caplog.records)
        assert not any(
            "before text/audio input is done" in record.message for record in caplog.records
        )
    finally:
        await agent.aclose()


async def test_empty_audio_and_late_text_do_not_release_next_segment(delivery):
    """空合成的两个通道分别结束，后续有效句只能消费自己的文本许可。

    显式模拟公开出生事件，不能靠字幕首次调用懒登记掩盖原轮次缺失。
    """
    output, participant, _ = delivery
    audio = ScheduledAudioOutput()
    sync = TranscriptSynchronizer(next_in_chain_audio=audio, next_in_chain_text=output)
    agent = NodeAgent()
    agent._delivery, agent._synchronizer = output, sync
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    gate = agent._audio_binding = _BoundAudioOutput(sync.audio_output, agent._audio_owner)
    try:
        for text, frames, delay in (
            ("没有音频的旧句。", empty_audio(), 0.02),
            ("真正播放的新句。", buffered_audio(), 0.005),
        ):
            await forward_generation(
                speech_handle=handle,
                audio_output=gate,
                text_output=sync.text_output,
                audio_source=frames,
                text_source=agent.transcription_node(delayed_text(text, delay), ModelSettings()),
                on_first_frame=lambda *_: None,
                reconcile_playout_pause=lambda: None,
            )
        await sync.barrier()
        await output.drain()
        assert ["".join(writer.chunks) for _, writer in participant.streams] == ["真正播放的新句。"]
        assert gate._permits == {}
    finally:
        await agent.aclose()


async def test_canceled_or_closed_audio_wait_cannot_be_released_by_old_text():
    """取消必须唤醒真实首帧等待；相同序号的旧token也不能重开新回复的闸。"""
    audio = ScheduledAudioOutput()
    owner = "old"
    gate = _BoundAudioOutput(audio, lambda: owner)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    old = gate.begin_text(owner)
    waiting = asyncio.create_task(gate.capture_frame(frame))
    await asyncio.sleep(0)
    gate.clear_buffer()
    gate.release(old, allowed=True)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, 0.2)
    assert audio.frames == []
    gate.flush()
    gate.finish_text(old)
    owner = "new"
    new = gate.begin_text(owner)
    waiting = asyncio.create_task(gate.capture_frame(frame))
    await asyncio.sleep(0)
    gate.release(old, allowed=True)
    assert not waiting.done()
    gate.release(new, allowed=True)
    await waiting
    gate.finish_text(new)
    gate.flush()
    await asyncio.wait_for(audio.finished.wait(), 0.2)
    assert len(audio.frames) == 1
    assert gate._permits == {}
    owner = "closing"
    closing = gate.begin_text(owner)
    waiting = asyncio.create_task(gate.capture_frame(frame))
    await asyncio.sleep(0)
    gate.close()
    gate.release(closing, allowed=True)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, 0.2)
    assert gate._permits == {}
    assert len(audio.frames) == 1


async def test_old_forward_finally_flush_cannot_close_new_reply_permit():
    """新输入可在旧首帧等待取消时到达，旧任务收尾必须保留自己的代次。"""
    audio = ScheduledAudioOutput()
    owner = "old"
    gate = _BoundAudioOutput(audio, lambda: owner)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    old = gate.begin_text(owner)

    async def forward_old():
        """沿用SDK音频forward的真实try/finally形状，不把flush移到新任务外。"""
        try:
            await gate.capture_frame(frame)
        finally:
            gate.flush()
            gate.clear_buffer()

    old_task = asyncio.create_task(forward_old())
    await asyncio.sleep(0)
    owner = "new"
    new = gate.begin_text(owner)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(old_task, 0.2)
    assert not new.audio_flushed
    assert gate._audio_index == 0
    gate.release(old, allowed=True)
    gate.release(new, allowed=True)
    await asyncio.wait_for(gate.capture_frame(frame), 0.2)
    gate.finish_text(new)
    gate.flush()
    await asyncio.wait_for(audio.finished.wait(), 0.2)
    assert len(audio.frames) == 1
    assert gate._permits == {}
    gate.close()


async def test_say_audio_task_created_before_text_node_inherits_reserved_identity(delivery, caplog):
    """say先创建音频任务，父作用域载体必须让先到的PCM等到自己的正文而非借用前句。

    显式模拟公开出生事件，不能靠字幕首次调用懒登记掩盖原轮次缺失。
    """
    output, participant, _ = delivery
    audio = ScheduledAudioOutput()
    sync = TranscriptSynchronizer(next_in_chain_audio=audio, next_in_chain_text=output)
    agent = NodeAgent()
    agent._delivery, agent._synchronizer = output, sync
    handle = SpeechHandle.create()
    agent.register_speech(handle)
    agent.test_session.current_speech = handle
    gate = agent._audio_binding = _BoundAudioOutput(sync.audio_output, agent._audio_owner)

    async def say_activity():
        """沿用SDK say的子任务创建顺序，并特意让首帧早于字幕节点运行。"""
        audio_task, audio_out = perform_audio_forwarding(
            audio_output=gate, tts_output=buffered_audio(), reconcile_playout_pause=lambda: None
        )
        await asyncio.sleep(0)
        assert audio.frames == [] and not audio_task.done()
        text_task, text_out = perform_text_forwarding(
            text_output=sync.text_output,
            source=agent.transcription_node(
                delayed_text("你好，很高兴见到你。", 0.02), ModelSettings()
            ),
        )
        try:
            await asyncio.wait_for(asyncio.gather(audio_task, text_task), 0.5)
            await asyncio.wait_for(gate.wait_for_playout(), 0.5)
        finally:
            audio_out.first_frame_fut.cancel()
            text_out.first_text_fut.cancel()

    try:
        with agent.announcement():
            await asyncio.create_task(say_activity())
        await sync.barrier()
        await output.drain()
        assert ["".join(writer.chunks) for _, writer in participant.streams] == [
            "你好，很高兴见到你。"
        ]
        assert len(audio.frames) == 1 and gate._permits == {}
        assert not any(
            "before text/audio input is done" in record.message for record in caplog.records
        )
    finally:
        await agent.aclose()


async def test_old_zero_frame_finally_cannot_flush_or_clear_new_reply():
    """SDK看门狗可在旧合成零帧时放行新handle，旧finally仍必须只持有旧身份。"""
    audio = ScheduledAudioOutput()
    owner = "old"
    gate = _BoundAudioOutput(audio, lambda: owner)
    old = gate.begin_text(owner)
    old_wait = asyncio.Event()

    async def old_zero_frame():
        """没有capture_frame也执行SDK相同收尾，不能依赖首帧登记任务身份。"""
        try:
            await old_wait.wait()
        finally:
            gate.flush()
            gate.clear_buffer()

    old_task = asyncio.create_task(old_zero_frame())
    await asyncio.sleep(0)
    owner = "new"
    new = gate.begin_text(owner)
    old_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task
    assert not new.audio_flushed and not new.ready.is_set()
    gate.release(old, allowed=True)
    gate.release(new, allowed=True)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    await asyncio.wait_for(gate.capture_frame(frame), 0.2)
    gate.finish_text(new)
    gate.flush()
    await asyncio.wait_for(audio.finished.wait(), 0.2)
    assert len(audio.frames) == 1 and gate._permits == {}
    gate.close()


async def test_close_wakes_announcement_first_frame_before_text_node_exists():
    """会话关闭不能依赖say先返回；尚未绑定的首帧等待也必须由同一所有者释放。"""
    audio = ScheduledAudioOutput()
    gate = _BoundAudioOutput(audio, lambda: "opening")
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    with gate.announcement():
        waiting = asyncio.create_task(gate.capture_frame(frame))
        await asyncio.sleep(0)
        gate.close()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            assert waiting.done()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert audio.frames == [] and gate._permits == {}


async def test_old_same_handle_clear_cannot_clear_collected_next_segment():
    """新段两路输入结束不代表设备播完，旧同handle收尾不能以空表为由清新播放。"""
    audio = ScheduledAudioOutput()
    gate = _BoundAudioOutput(audio, lambda: "same")
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    old = gate.begin_text("same")
    clear_old = asyncio.Event()

    async def delayed_old_clear():
        """旧任务继承首段载体，在新段collect后才执行最后一个取消清理。"""
        await clear_old.wait()
        gate.clear_buffer()

    old_task = asyncio.create_task(delayed_old_clear())
    gate.release(old, allowed=True)
    await gate.capture_frame(frame)
    gate.finish_text(old)
    gate.flush()
    await asyncio.wait_for(audio.finished.wait(), 0.2)
    audio.finished.clear()
    new = gate.begin_text("same")
    gate.release(new, allowed=True)
    await gate.capture_frame(frame)
    gate.finish_text(new)
    gate.flush()
    assert gate._permits == {} and audio.started
    clear_old.set()
    await old_task
    assert audio.clears == 0
    gate.clear_buffer()
    assert audio.clears == 1
    await asyncio.wait_for(audio.finished.wait(), 0.2)
    gate.close()
