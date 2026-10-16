"""用正式 Silero 事件和内存上传验证字节边界，禁止加载模型权重或联网。"""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import utils, vad
from livekit.plugins.silero import vad as silero_vad

from xiaoya.infrastructure.streaming_stt import LocalRecognitionStream


class FixedProbability:
    """概率仅用于触发确定性窗口，不能把它当成真实语音识别或声学结果。"""

    sample_rate = 16000
    window_size_samples = 512

    def __init__(self, values):
        """保留真实 SDK 状态机，避免手写 START/END 夹具遗漏事件顺序。"""
        self.values = iter(values)

    def __call__(self, _samples):
        """每个实际推理窗口消费一个固定概率，不使用真实权重。"""
        return next(self.values)

    def reset(self):
        """允许 SDK 真正执行 flush，不能由假模型额外补 END 掩盖丢尾。"""


class MemorySocket:
    """按实际 commit 保存字节，final 可以挂起以验证取消与失败。"""

    def __init__(self, owner, *, acknowledge=True, fail_at=None):
        """不创建传输对象，检查的是正式发送器产生的顺序和原始字节。"""
        self.owner = owner
        self.acknowledge = acknowledge
        self.fail_at = fail_at
        self.blocks = []
        self.pending = bytearray()
        self.segments = []
        self.controls = []
        self.commit_entered = asyncio.Event()

    async def send_bytes(self, data):
        """发送异常必须直接传播，不能自动补 commit/end 或重放音频。"""
        if self.fail_at == len(self.blocks):
            raise OSError("controlled-send-failure")
        self.blocks.append(data)
        self.pending.extend(data)

    async def send_json(self, value):
        """模拟一次服务端确认，不生成文字或修改上传字节。"""
        self.controls.append(value["type"])
        if value["type"] == "commit":
            self.segments.append(bytes(self.pending))
            self.pending.clear()
            self.commit_entered.set()
            if self.acknowledge:
                self.owner._pending_final.set_result(None)


def boundary(probabilities, *, acknowledge=True, fail_at=None):
    """绕开 STT 握手任务但保留正式 VADStream 与生产 feed/send 两个协程。"""
    opts = silero_vad._VADOptions(
        min_speech_duration=0.05,
        min_silence_duration=0.30,
        prefix_padding_duration=0.5,
        max_buffered_speech=60,
        activation_threshold=0.5,
        deactivation_threshold=0.35,
        sample_rate=16000,
    )
    detector = silero_vad.VADStream(
        silero_vad.VAD(session=None, opts=opts), opts, FixedProbability(probabilities)
    )
    stream = LocalRecognitionStream.__new__(LocalRecognitionStream)
    stream._event_ch = utils.aio.Chan()
    stream._input_ch = utils.aio.Chan()
    stream._pending_final = None
    stream._speech_end_time = None
    stream._conn_options = SimpleNamespace(timeout=0.1)
    socket = MemorySocket(stream, acknowledge=acknowledge, fail_at=fail_at)
    return SimpleNamespace(stream=stream, detector=detector, socket=socket, events=[])


async def run_boundary(case, samples, *, chunk_samples=512, flush_at_sample=None):
    """输入完整原字节及 SDK 正常 EOF 标记，finally 回收真实 VAD 后台任务。"""
    for start in range(0, len(samples), chunk_samples):
        if start == flush_at_sample:
            case.stream._input_ch.send_nowait(case.stream._FlushSentinel())
        part = samples[start : start + chunk_samples]
        case.stream._input_ch.send_nowait(
            rtc.AudioFrame(
                data=part.tobytes(),
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=len(part),
            )
        )
    case.stream._input_ch.send_nowait(case.stream._FlushSentinel())
    case.stream._input_ch.close()

    async def events():
        """保存真实事件用于核对，不改变先 INFERENCE 再 START/END 的顺序。"""
        async for event in case.detector:
            case.events.append(event)
            yield event

    tasks = [
        asyncio.create_task(case.stream._feed_vad(case.detector)),
        asyncio.create_task(case.stream._send_audio(events(), case.socket)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        await utils.aio.cancel_and_wait(*tasks)
        await case.detector.aclose()


def indexed_samples(count):
    """使用可比较的原始字节序列，避免靠字数或重复内容判断音频是否完整。"""
    return (np.arange(count, dtype=np.int64) % 32000).astype("<i2")


@pytest.mark.parametrize("chunk_samples", [160, 512, 777])
async def test_normal_vad_segment_uploads_original_bytes_once(chunk_samples):
    """单段前导及触发 END 的最后窗口都要精确保留，不能重发完整 END 缓存。"""
    case = boundary([0.0] * 10 + [1.0] * 20 + [0.0] * 20)
    samples = indexed_samples(50 * 512)
    await run_boundary(case, samples, chunk_samples=chunk_samples)
    start = next(e for e in case.events if e.type == vad.VADEventType.START_OF_SPEECH)
    end = next(e for e in case.events if e.type == vad.VADEventType.END_OF_SPEECH)
    prefix = start.samples_index - sum(f.samples_per_channel for f in start.frames)
    assert case.socket.segments == [samples[prefix : end.samples_index].tobytes()]
    assert case.socket.controls == ["commit", "end"]


@pytest.mark.parametrize("chunk_samples", [160, 512, 777])
async def test_next_commit_does_not_repeat_previous_speech_prefix(chunk_samples):
    """500ms 前导长于300ms分段静音，下一缓存不能再次识别上段已上传尾音。"""
    case = boundary([0.0] * 10 + [1.0] * 20 + [0.0] * 12 + [1.0] * 20 + [0.0] * 20)
    samples = indexed_samples(82 * 512)
    await run_boundary(case, samples, chunk_samples=chunk_samples)
    starts = [e for e in case.events if e.type == vad.VADEventType.START_OF_SPEECH]
    ends = [e for e in case.events if e.type == vad.VADEventType.END_OF_SPEECH]
    assert len(starts) == len(ends) == len(case.socket.segments) == 2
    prefix0 = starts[0].samples_index - sum(f.samples_per_channel for f in starts[0].frames)
    prefix1 = starts[1].samples_index - sum(f.samples_per_channel for f in starts[1].frames)
    assert prefix1 < ends[0].samples_index, "必须真的覆盖跨 commit 前导重叠反例"
    assert case.socket.segments[0] == samples[prefix0 : ends[0].samples_index].tobytes()
    assert (
        case.socket.segments[1] == samples[ends[0].samples_index : ends[1].samples_index].tobytes()
    )
    assert case.socket.controls == ["commit", "commit", "end"]


@pytest.mark.parametrize("tail_samples", [0, 1, 237, 511])
async def test_normal_input_end_commits_active_speech_and_original_short_tail(tail_samples):
    """没有300ms静音的正常EOF也应final，且不足一个VAD窗口的原尾音不能被flush吞掉。"""
    case = boundary([0.0] * 10 + [1.0] * 20)
    samples = indexed_samples(30 * 512 + tail_samples)
    await run_boundary(case, samples, chunk_samples=160)
    start = next(e for e in case.events if e.type == vad.VADEventType.START_OF_SPEECH)
    prefix = start.samples_index - sum(f.samples_per_channel for f in start.frames)
    assert case.socket.controls == ["commit", "end"]
    assert case.socket.segments == [samples[prefix:].tobytes()]


@pytest.mark.parametrize("has_silence_end", [True, False])
async def test_cancel_during_final_confirmation_never_sends_end_or_retries(has_silence_end):
    """已上传的音频不能因取消后finally收尾而被重复提交或假装正常结束。"""
    probabilities = [0.0] * 10 + [1.0] * 20 + ([0.0] * 20 if has_silence_end else [])
    case = boundary(probabilities, acknowledge=False)
    task = asyncio.create_task(run_boundary(case, indexed_samples(len(probabilities) * 512)))
    await asyncio.wait_for(case.socket.commit_entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert case.socket.controls == ["commit"]
    assert len(case.socket.segments) == 1


async def test_send_failure_propagates_without_commit_end_or_replay():
    """传输中断不是音轨正常EOF，不能追加final或重试掩盖已知发送失败。"""
    case = boundary([0.0] * 10 + [1.0] * 20 + [0.0] * 20, fail_at=1)
    with pytest.raises(OSError, match="controlled-send-failure"):
        await run_boundary(case, indexed_samples(50 * 512))
    assert len(case.socket.blocks) == 1
    assert case.socket.controls == []


async def test_missing_final_confirmation_propagates_timeout_without_end():
    """final未确认时保持未知失败，不把已发commit当作可以安全结束的成功。"""
    case = boundary([0.0] * 10 + [1.0] * 20 + [0.0] * 20, acknowledge=False)
    with pytest.raises(TimeoutError):
        await run_boundary(case, indexed_samples(50 * 512))
    assert case.socket.controls == ["commit"]


async def test_completed_epoch_flush_resets_cursor_without_dropping_next_start():
    """显式 flush 后 SDK 样本坐标归零，旧上传游标不能吞掉新段；不声称覆盖 active flush。"""
    pattern = [0.0] * 10 + [1.0] * 20 + [0.0] * 20
    case = boundary(pattern + pattern)
    samples = indexed_samples(100 * 512)
    await run_boundary(case, samples, flush_at_sample=50 * 512)
    assert case.socket.controls == ["commit", "commit", "end"]
    assert case.socket.segments == [
        samples[: 40 * 512].tobytes(),
        samples[50 * 512 : 90 * 512].tobytes(),
    ]


@pytest.mark.parametrize("sample_count", [0, 1, 511, 1024])
async def test_empty_or_unvoiced_input_never_invents_speech_commit(sample_count):
    """EOF补提交仅针对已经 START 的语音，空帧或纯静音不得绕过VAD门创建识别段。"""
    case = boundary([0.0] * (sample_count // 512))
    await run_boundary(case, np.zeros(sample_count, dtype="<i2"), chunk_samples=160)
    assert case.socket.controls == ["end"]
    assert case.socket.blocks == case.socket.segments == []


async def test_multichannel_input_is_rejected_before_tail_coordinate_association():
    """生产坐标固定16k单声道，多声道不能被悄悄当成双倍尾样本再次发送。"""
    case = boundary([])
    case.stream._input_ch.send_nowait(
        rtc.AudioFrame(data=bytes(640), sample_rate=16000, num_channels=2, samples_per_channel=160)
    )
    case.stream._input_ch.close()
    try:
        with pytest.raises(ValueError, match="单声道"):
            await case.stream._feed_vad(case.detector)
    finally:
        await case.detector.aclose()
