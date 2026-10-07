"""假房间只替换公开传输边界，验证表达许可不会泄漏到未播放或已取消的句段。"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from livekit import rtc
from livekit.agents.voice import io
from livekit.agents.voice.transcription import TranscriptSynchronizer

from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.infrastructure.delivery_output import (
    DELIVERY_SNAPSHOT_RPC,
    DELIVERY_TOPIC,
    TRANSCRIPTION_TOPIC,
    DeliveryTextOutput,
)


class FakeWriter:
    """保留正文与关闭头，方便检查字幕失败与语义结束属于不同边界。"""

    def __init__(self) -> None:
        """每个假流独占错误开关，模拟单段故障后下一段可以恢复。"""
        self.chunks: list[str] = []
        self.close_attributes: list[dict[str, str]] = []
        self.write_error = False
        self.close_error = False

    async def write(self, text: str) -> None:
        """失败时不保留正文，用于验证服务端不把失败误当可继续的字幕流。"""
        if self.write_error:
            raise ConnectionError("模拟字幕写入失败")
        self.chunks.append(text)

    async def aclose(self, *, attributes: dict[str, str]) -> None:
        """关闭错误发生在调用已被消费后，重复关闭会被测试计数发现。"""
        self.close_attributes.append(dict(attributes))
        if self.close_error:
            raise ConnectionError("模拟字幕关闭失败")


class FakeParticipant:
    """模拟公开 API，不依赖任何 SDK 内部输出类或真实 FFI 房间。"""

    def __init__(self) -> None:
        """显式记录尝试和成功，区别网络不确定性与本地权威版本。"""
        self.handlers: dict[str, Any] = {}
        self.unregistered: list[str] = []
        self.packets: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.streams: list[tuple[dict[str, str], FakeWriter]] = []
        self.publish_failures = 0
        self.stream_failures = 0
        self.register_error = False
        self.publish_gate: asyncio.Event | None = None
        self.publish_started = asyncio.Event()
        self.stream_started = asyncio.Event()

    def register_rpc_method(self, name: str, handler: Any) -> None:
        """保存真实回调，让权限测试经过与生产相同的入口。"""
        if self.register_error:
            raise ConnectionError("模拟 RPC 注册失败")
        self.handlers[name] = handler

    def unregister_rpc_method(self, name: str) -> None:
        """记录幂等关闭是否重复撤销房间方法。"""
        self.unregistered.append(name)
        self.handlers.pop(name)

    async def publish_data(self, payload: str, *, reliable: bool, topic: str) -> None:
        """可控阻塞把取消精确放到网络 await 中间，而不需要真实计时或服务。"""
        assert reliable is True
        assert topic == DELIVERY_TOPIC
        packet = json.loads(payload)
        self.attempts.append(packet)
        self.publish_started.set()
        if self.publish_gate is not None:
            await self.publish_gate.wait()
        if self.publish_failures:
            self.publish_failures -= 1
            raise ConnectionError("模拟可靠消息失败")
        self.packets.append(packet)

    async def stream_text(self, *, topic: str, attributes: dict[str, str]) -> FakeWriter:
        """断言仍使用标准字幕 topic，避免另建通道破坏已有消息展示。"""
        assert topic == TRANSCRIPTION_TOPIC
        if self.stream_failures:
            self.stream_failures -= 1
            raise ConnectionError("模拟字幕开流失败")
        writer = FakeWriter()
        self.streams.append((dict(attributes), writer))
        self.stream_started.set()
        return writer


class MemoryAudioOutput(io.AudioOutput):
    """使用 SDK 真实音频输出基类，只将设备播放替换为测试显式触发的公开事件。"""

    def __init__(self) -> None:
        """内存音频不打开声卡，真实同步器仍负责它与字幕的播放许可关系。"""
        super().__init__(label="test-memory-audio", capabilities=io.AudioOutputCapabilities(False))
        self.frames: list[rtc.AudioFrame] = []

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """沿用基类的段计数，让公开 playback_finished 事件经过 SDK 正常校验。"""
        await super().capture_frame(frame)
        self.frames.append(frame)

    def flush(self) -> None:
        """合成完成只结束输入，测试必须另行确认实际播放完成。"""
        super().flush()

    def clear_buffer(self) -> None:
        """内存设备没有真实播放队列，打断事件由测试显式提供实际播放位置。"""


@pytest.fixture
async def delivery() -> AsyncIterator[tuple[DeliveryTextOutput, FakeParticipant, SimpleNamespace]]:
    """每项测试都关闭后台 worker，测试进程退出不能掩盖输出资源泄漏。"""
    participant = FakeParticipant()
    room = SimpleNamespace(
        local_participant=participant,
        remote_participants={"human": SimpleNamespace(identity="human")},
    )
    output = DeliveryTextOutput(room, instance="test-instance", io_timeout=0.02)
    yield output, participant, room
    await output.aclose()


async def test_binding_and_empty_segment_never_grant_playback(delivery: tuple) -> None:
    """预生成、纯空白和无正文 flush 都不能让浏览器提前做动作。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "wave"))
    assert output.snapshot.state == "closed"
    assert participant.attempts == []
    await output.capture_text(" \n")
    output.flush()
    await output.drain()
    assert output.snapshot.revision == 0
    assert participant.streams == []
    assert participant.attempts == []


async def test_first_text_headers_and_state_share_one_revision(delivery: tuple) -> None:
    """同一不可变快照同时发给字幕与状态通道，浏览器无需猜文本或时钟关系。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("太好了，")
    await output.capture_text("我们继续。")
    await output.drain()
    attributes, writer = participant.streams[0]
    header = json.loads(attributes[DELIVERY_TOPIC])
    assert header == participant.packets[0] == json.loads(output.snapshot.to_json())
    assert header == {
        "v": 1,
        "instance": "test-instance",
        "revision": 1,
        "reply_id": "reply-1",
        "segment_id": attributes["lk.segment_id"],
        "state": "active",
        "style": "happy",
        "gesture": "nod",
    }
    assert json.loads(attributes["lk.expression"]) == {"expression": "happy", "mood": "happy"}
    assert "".join(writer.chunks) == "太好了，我们继续。"
    output.flush()
    await output.drain()
    assert output.snapshot.state == "closed"
    assert output.snapshot.revision == 2
    assert (output.snapshot.style, output.snapshot.gesture) == ("neutral", "none")
    assert writer.close_attributes == [{"lk.transcription_final": "true"}]


async def test_queued_segments_keep_immutable_metadata(delivery: tuple) -> None:
    """异步关闭尚未完成时绑定下一段，也不能把下一段风格写到上一段的开头。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("好消息。")
    await output.drain()
    output.flush()
    await output.bind_segment("reply-1", DeliveryIntent("gentle", "none"))
    await output.capture_text("慢慢来。")
    await output.drain()
    headers = [json.loads(attributes[DELIVERY_TOPIC]) for attributes, _ in participant.streams]
    assert [header["style"] for header in headers] == ["happy", "gentle"]
    assert [header["reply_id"] for header in headers] == ["reply-1", "reply-1"]
    assert headers[0]["segment_id"] != headers[1]["segment_id"]
    output.flush()
    await output.drain()
    assert [packet["revision"] for packet in participant.packets] == [1, 2, 3, 4]


async def test_cancelled_binding_and_queued_text_never_activate(delivery: tuple) -> None:
    """取消标记在等待工作队列前生效，排队但未播出的旧句段不会泄漏字幕或意图。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "wave"))
    await output.capture_text("尚未输出。")
    await output.cancel_reply("reply-1")
    await output.capture_text("迟到的正文。")
    output.flush()
    await output.drain()
    assert participant.packets == []
    assert participant.streams == []


async def test_cancellation_during_active_publish_does_not_open_text(delivery: tuple) -> None:
    """网络已在发送的 active 可以随后关闭，但取消之后不能再发正文头让它生效。"""
    output, participant, _ = delivery
    participant.publish_gate = asyncio.Event()
    await output.bind_segment("reply-1", DeliveryIntent("happy", "wave"))
    await output.capture_text("不会被公开。")
    await participant.publish_started.wait()
    cancel = asyncio.create_task(output.cancel_reply("reply-1"))
    await asyncio.sleep(0)
    participant.publish_gate.set()
    await cancel
    await output.drain()
    assert participant.streams == []
    assert output.snapshot.state == "closed"
    assert [packet["state"] for packet in participant.packets] == ["active", "closed"]


async def test_late_cancel_cannot_close_a_new_reply(delivery: tuple) -> None:
    """旧 SpeechHandle 回调晚到时只淘汰旧身份，新回复仍拥有自己的 active 版本。"""
    output, participant, _ = delivery
    await output.bind_segment("old", DeliveryIntent("happy", "nod"))
    await output.capture_text("旧回复。")
    output.flush()
    await output.bind_segment("new", DeliveryIntent("curious", "tilt"))
    await output.capture_text("新问题？")
    await output.drain()
    latest = output.snapshot
    await output.cancel_reply("old")
    assert output.snapshot == latest
    assert output.snapshot.reply_id == "new"
    assert participant.streams[-1][1].close_attributes == []


async def test_state_publish_failure_keeps_plain_subtitles(delivery: tuple) -> None:
    """表达通知失败不能阻止普通字幕，但字幕头不能携带仍有效的 active 许可。"""
    output, participant, _ = delivery
    participant.publish_failures = 1
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("正文仍然可见。")
    await output.drain()
    attributes, writer = participant.streams[0]
    assert writer.chunks == ["正文仍然可见。"]
    assert json.loads(attributes[DELIVERY_TOPIC])["state"] == "closed"
    assert json.loads(attributes["lk.expression"])["expression"] == "neutral"
    assert output.snapshot.state == "closed"


async def test_stream_open_failure_closes_expression_and_next_segment_recovers(
    delivery: tuple,
) -> None:
    """单段字幕开流失败不让表情悬挂，也不把下一段永久锁死在故障状态。"""
    output, participant, _ = delivery
    participant.stream_failures = 1
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("失败段。")
    await output.drain()
    assert output.snapshot.state == "closed"
    output.flush()
    await output.bind_segment("reply-2", DeliveryIntent("gentle", "none"))
    await output.capture_text("恢复段。")
    await output.drain()
    assert output.snapshot.reply_id == "reply-2"
    assert output.snapshot.state == "active"
    assert participant.streams[0][1].chunks == ["恢复段。"]


async def test_write_and_close_failures_revoke_expression_without_raising(delivery: tuple) -> None:
    """正文或收尾故障只结束当前表达，重复 flush 不会再次消费坏掉的 writer。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("开始。")
    await output.drain()
    writer = participant.streams[0][1]
    writer.write_error = True
    writer.close_error = True
    await output.capture_text("失败正文。")
    await output.capture_text("不重试坏流。")
    output.flush()
    await output.drain()
    assert output.snapshot.state == "closed"
    assert writer.chunks == ["开始。"]
    assert len(writer.close_attributes) == 1


async def test_rpc_authenticates_live_caller_not_payload_identity(delivery: tuple) -> None:
    """只允许仍在房间内的 SDK 调用者，正文伪造合法 identity 不能绕过校验。"""
    output, participant, room = delivery
    handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
    accepted = await handler(SimpleNamespace(caller_identity="human", payload="not-json"))
    assert json.loads(accepted) == json.loads(output.snapshot.to_json())
    with pytest.raises(rtc.RpcError):
        await handler(SimpleNamespace(caller_identity="outsider", payload='{"identity":"human"}'))
    room.remote_participants.clear()
    with pytest.raises(rtc.RpcError):
        await handler(SimpleNamespace(caller_identity="human", payload="{}"))


async def test_network_timeout_does_not_block_capture_or_leave_active(delivery: tuple) -> None:
    """不依赖真实时钟同步，有限超时后关闭表达；音频调用方不等待控制网络。"""
    output, participant, _ = delivery
    participant.publish_gate = asyncio.Event()
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("超时仍显示字幕。")
    assert participant.attempts == []
    await asyncio.wait_for(output.drain(), timeout=0.5)
    assert output.snapshot.state == "closed"
    assert participant.streams[0][1].chunks == ["超时仍显示字幕。"]


async def test_close_is_idempotent_and_discards_queued_output(delivery: tuple) -> None:
    """会话关闭时丢弃过期排队正文，注销一次 RPC 并停止后台任务。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("已发正文。")
    await output.drain()
    await asyncio.gather(output.aclose(), output.aclose())
    await output.capture_text("关闭后的正文。")
    await output.bind_segment("reply-2", DeliveryIntent())
    await output.cancel_reply("reply-1")
    output.flush()
    await output.drain()
    assert participant.unregistered == [DELIVERY_SNAPSHOT_RPC]
    assert participant.handlers == {}
    assert participant.streams[0][1].chunks == ["已发正文。"]
    assert len(participant.streams[0][1].close_attributes) == 1
    assert output.snapshot.state == "closed"


async def test_missing_binding_preserves_plain_subtitles_without_expression(
    delivery: tuple,
) -> None:
    """装配遗漏不能猜当前 SpeechHandle，字幕可继续但不生成假的表达身份。"""
    output, participant, _ = delivery
    await output.capture_text("普通字幕。")
    output.flush()
    await output.drain()
    attributes, writer = participant.streams[0]
    assert DELIVERY_TOPIC not in attributes
    assert writer.chunks == ["普通字幕。"]
    assert participant.packets == []


async def test_rpc_registration_failure_disables_expression_not_subtitles() -> None:
    """无法提供重连快照时不授予无法追溯的 active，通话正文仍尽力输出。"""
    participant = FakeParticipant()
    participant.register_error = True
    room = SimpleNamespace(local_participant=participant, remote_participants={})
    output = DeliveryTextOutput(room)
    try:
        await output.bind_segment("reply-1", DeliveryIntent("happy", "wave"))
        await output.capture_text("普通回复。")
        output.flush()
        await output.drain()
        assert participant.packets == []
        assert participant.streams[0][1].chunks == ["普通回复。"]
        assert output.snapshot.state == "closed"
    finally:
        await output.aclose()


async def test_shutdown_cancels_a_stalled_publish_and_drains_pending_operations(
    delivery: tuple,
) -> None:
    """关闭不必等待控制网络恢复，未发送的正文和等待取消的 future 都能结束。"""
    output, participant, _ = delivery
    participant.publish_gate = asyncio.Event()
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("不会在结束后补发。")
    await participant.publish_started.wait()
    cancel = asyncio.create_task(output.cancel_reply("reply-1"))
    await asyncio.sleep(0)
    await asyncio.wait_for(output.aclose(), timeout=0.5)
    await asyncio.wait_for(cancel, timeout=0.5)
    await asyncio.wait_for(output.drain(), timeout=0.5)
    assert output.snapshot.state == "closed"
    assert participant.streams == []
    assert participant.handlers == {}


async def test_shutdown_during_flush_still_closes_the_owned_writer(delivery: tuple) -> None:
    """收尾撤销状态时若被关闭取消，writer 仍由输出层拥有，不能遗失回收机会。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("已发送。")
    await output.drain()
    writer = participant.streams[0][1]
    participant.publish_gate = asyncio.Event()
    participant.publish_started = asyncio.Event()
    output.flush()
    await participant.publish_started.wait()
    await asyncio.wait_for(output.aclose(), timeout=0.5)
    assert writer.close_attributes == [{"lk.transcription_final": "true"}]
    assert output.snapshot.state == "closed"


async def test_expired_queued_segment_keeps_text_but_never_activates(delivery: tuple) -> None:
    """已经播放结束的排队正文可以补字幕，但不能在新回复开始后补做旧手势。"""
    output, participant, _ = delivery
    await output.bind_segment("old", DeliveryIntent("happy", "wave"))
    await output.capture_text("上一段已经结束。")
    output.flush()
    await output.bind_segment("new", DeliveryIntent("gentle", "none"))
    await output.capture_text("正在播放的回复。")
    await output.drain()
    old_attributes, old_writer = participant.streams[0]
    old_header = json.loads(old_attributes[DELIVERY_TOPIC])
    assert old_header["reply_id"] == "old"
    assert (old_header["state"], old_header["style"], old_header["gesture"]) == (
        "closed",
        "neutral",
        "none",
    )
    assert old_writer.chunks == ["上一段已经结束。"]
    assert not any(
        packet["reply_id"] == "old" and packet["state"] == "active"
        for packet in participant.packets
    )
    assert output.snapshot.reply_id == "new"
    assert output.snapshot.state == "active"


async def test_real_sdk_synchronizer_waits_for_playback_and_rotates_bound_segments(
    delivery: tuple,
) -> None:
    """真实 SDK 屏蔽播放前字幕，两个公开段边界后仍保持各自回复和字幕身份。"""
    output, participant, _ = delivery
    audio = MemoryAudioOutput()
    synchronizer = TranscriptSynchronizer(next_in_chain_audio=audio, next_in_chain_text=output)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    try:
        for reply_id, style, text in (
            ("first", "happy", "您好，今天真好。"),
            ("second", "gentle", "我们慢慢说吧。"),
        ):
            await synchronizer.barrier()
            await output.bind_segment(reply_id, DeliveryIntent(style, "none"))
            participant.stream_started = asyncio.Event()
            streams_before = len(participant.streams)
            await synchronizer.text_output.capture_text(text)
            synchronizer.text_output.flush()
            await synchronizer.audio_output.capture_frame(frame)
            synchronizer.audio_output.flush()
            await asyncio.sleep(0)
            assert len(participant.streams) == streams_before
            audio.on_playback_started(created_at=time.time())
            await asyncio.wait_for(participant.stream_started.wait(), timeout=1.0)
            assert any(
                packet["reply_id"] == reply_id and packet["state"] == "active"
                for packet in participant.packets
            )
            audio.on_playback_finished(playback_position=frame.duration, interrupted=False)
            await synchronizer.barrier()
            await output.drain()
            attributes, writer = participant.streams[-1]
            assert json.loads(attributes[DELIVERY_TOPIC])["reply_id"] == reply_id
            assert "".join(writer.chunks) == text
            assert output.snapshot.reply_id == reply_id
            assert output.snapshot.state == "closed"
        ids = [attributes["lk.segment_id"] for attributes, _ in participant.streams]
        assert len(set(ids)) == 2
        assert len(audio.frames) == 2
    finally:
        await synchronizer.aclose()


async def test_real_sdk_cancel_before_playback_never_leaks_prepared_text(delivery: tuple) -> None:
    """即便音频与字幕已送入真实同步器，未开始播放就取消仍不产生正文或动作。"""
    output, participant, _ = delivery
    audio = MemoryAudioOutput()
    synchronizer = TranscriptSynchronizer(next_in_chain_audio=audio, next_in_chain_text=output)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=2400)
    try:
        await output.bind_segment("cancelled", DeliveryIntent("happy", "wave"))
        await synchronizer.text_output.capture_text("提前准备但不会播出的回复。")
        synchronizer.text_output.flush()
        await synchronizer.audio_output.capture_frame(frame)
        synchronizer.audio_output.flush()
        await output.cancel_reply("cancelled")
        audio.on_playback_finished(playback_position=0, interrupted=True)
        await synchronizer.barrier()
        await output.drain()
        assert participant.packets == []
        assert participant.streams == []
        assert output.snapshot.state == "closed"
    finally:
        await synchronizer.aclose()
