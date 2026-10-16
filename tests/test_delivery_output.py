"""假房间只替换公开传输边界，验证表达许可不会泄漏到未播放或已取消的句段。"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from livekit import rtc
from livekit.agents.voice import io
from livekit.agents.voice.transcription import TranscriptSynchronizer

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationCue
from xiaoya.infrastructure.delivery_output import (
    DELIVERY_SNAPSHOT_RPC,
    DELIVERY_TOPIC,
    PRESENTATION_ACK_RPC,
    TRANSCRIPTION_TOPIC,
    DeliverySnapshotEndpoint,
    DeliveryState,
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
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.last_handler: Any = None
        self.packets: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.streams: list[tuple[dict[str, str], FakeWriter]] = []
        self.publish_failures = 0
        self.stream_failures = 0
        self.register_error = False
        self.register_after_install_error = False
        self.unregister_error = False
        self.publish_gate: asyncio.Event | None = None
        self.publish_started = asyncio.Event()
        self.stream_started = asyncio.Event()

    def register_rpc_method(self, name: str, handler: Any) -> None:
        """记录唯一注册及部分失败，让共享所有权和迟到回调经过生产入口验证。"""
        self.registered.append(name)
        if self.register_error:
            raise ConnectionError("模拟 RPC 注册失败")
        self.handlers[name] = handler
        self.last_handler = handler
        if self.register_after_install_error:
            raise ConnectionError("模拟保存回调后的注册失败")

    def unregister_rpc_method(self, name: str) -> None:
        """即使注销失败仍可捕获旧回调，不能以方法已移除代替应用层撤销授权。"""
        self.unregistered.append(name)
        if self.unregister_error:
            raise ConnectionError("模拟注销失败")
        self.handlers.pop(name, None)

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
    endpoint = DeliverySnapshotEndpoint(
        room, ready=lambda _identity: True, state=lambda: output.snapshot, instance="test-instance"
    )
    output = DeliveryTextOutput(room, snapshot_endpoint=endpoint, io_timeout=0.02)
    try:
        yield output, participant, room
    finally:
        await output.aclose()
        await endpoint.aclose()


def snapshot_room() -> tuple[FakeParticipant, SimpleNamespace]:
    """共享就绪测试只替换公开房间边界，当前参与者表仍是鉴权的必要证据。"""
    participant = FakeParticipant()
    room = SimpleNamespace(
        local_participant=participant,
        remote_participants={"human": SimpleNamespace(identity="human")},
    )
    return participant, room


async def test_command_delivery_does_not_require_surviving_audio_binding(delivery) -> None:
    """音频取消不能吞掉仍有效的指令更新，嘴部许可仍由前端独立核验真实音轨。"""
    output, participant, _ = delivery
    output.present(
        PresentationCue(
            phase="speaking",
            turn_id=output._presentation.turn_id,
            reply_id="ended",
            segment_id="ended-segment",
            intent=DeliveryIntent("happy", "wave"),
            gesture_id="command-id",
            gesture_source="command",
        )
    )
    await output.drain()
    assert output.snapshot.gesture_source == "command"
    assert participant.packets[-1]["gesture_id"] == "command-id"


async def test_sdk_idle_before_handle_done_preserves_completed_history(delivery) -> None:
    """真实SDK先发idle再handle done，独立正文候选必须越过视觉撤销且仅正常done封存。"""
    output, _, _ = delivery
    output._presentation.complete_turn("真实用户问题")
    await output.bind_segment("reply", DeliveryIntent(), "完整回复。")
    await output.capture_text("完整回复。")
    output.flush()
    await output.drain()
    output._presentation.phase("idle")
    assert output._playback_binding is None
    assert output._presentation._recent_assistant == ""
    await output.finish_reply("reply")
    assert output._presentation._recent_user == "真实用户问题"
    assert output._presentation._recent_assistant == "完整回复。"


async def test_shared_endpoint_before_output_returns_stable_neutral() -> None:
    """关闭表现时不构造字幕输出或发送动作，成功快照仍能证明稳定的当前会话就绪。"""
    participant, room = snapshot_room()
    provider = Mock(return_value=None)
    endpoint = DeliverySnapshotEndpoint(room, ready=Mock(return_value=True), state=provider)
    try:
        handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
        first = json.loads(await handler(SimpleNamespace(caller_identity="human", payload="")))
        second = json.loads(await handler(SimpleNamespace(caller_identity="human", payload="")))
        assert first == second == json.loads(DeliveryState(instance=endpoint.instance).to_json())
        assert endpoint.registered
        assert participant.registered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]
        assert participant.packets == participant.streams == []
    finally:
        await endpoint.aclose()


async def test_shared_endpoint_not_ready_never_reads_expression_state() -> None:
    """状态提供者只在业务和房内身份都通过后访问，提前注册不等于提前成功。"""
    participant, room = snapshot_room()
    ready, provider = Mock(return_value=False), Mock(return_value=None)
    endpoint = DeliverySnapshotEndpoint(room, ready=ready, state=provider)
    try:
        handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
        with pytest.raises(rtc.RpcError) as rejected:
            await handler(SimpleNamespace(caller_identity="human", payload=""))
        assert rejected.value.code == 2001
        provider.assert_not_called()
        ready.assert_called_once_with("human")
        ready.return_value = True
        assert (
            json.loads(await handler(SimpleNamespace(caller_identity="human", payload="")))["state"]
            == "closed"
        )
        provider.assert_called_once_with()
    finally:
        await endpoint.aclose()


@pytest.mark.parametrize("caller", ["", "outsider"])
async def test_shared_endpoint_rejects_payload_identity_before_ready(caller: str) -> None:
    """传输身份缺失或房内不可见时，正文声称已绑定用户也不能触发就绪或状态读取。"""
    participant, room = snapshot_room()
    ready, provider = Mock(return_value=True), Mock(return_value=None)
    endpoint = DeliverySnapshotEndpoint(room, ready=ready, state=provider)
    try:
        with pytest.raises(rtc.RpcError) as rejected:
            await participant.handlers[DELIVERY_SNAPSHOT_RPC](
                SimpleNamespace(caller_identity=caller, payload='{"identity":"human"}')
            )
        assert rejected.value.code == 2001
        ready.assert_not_called()
        provider.assert_not_called()
    finally:
        await endpoint.aclose()


async def test_shared_endpoint_provider_failure_keeps_readiness_neutral(caplog) -> None:
    """已授权会话不因装饰状态故障失去就绪，日志也不能复制异常中的正文。"""
    participant, room = snapshot_room()
    provider = Mock(side_effect=RuntimeError("禁止复制的模拟正文"))
    endpoint = DeliverySnapshotEndpoint(room, ready=Mock(return_value=True), state=provider)
    try:
        handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
        failed = json.loads(await handler(SimpleNamespace(caller_identity="human", payload="")))
        assert failed == json.loads(DeliveryState(instance=endpoint.instance).to_json())
        assert "禁止复制的模拟正文" not in caplog.text
        provider.side_effect = None
        provider.return_value = DeliveryState(instance="other-job", state="active")
        assert (
            json.loads(await handler(SimpleNamespace(caller_identity="human", payload="")))
            == failed
        )
        assert endpoint.registered
    finally:
        await endpoint.aclose()


@pytest.mark.parametrize("partial", [False, True])
async def test_shared_endpoint_registration_failure_propagates_and_revokes(partial: bool) -> None:
    """共享入口属于启动门；注册完全或部分失败都必须上抛且撤销已捕获的回调。"""
    participant, room = snapshot_room()
    participant.register_error = not partial
    participant.register_after_install_error = partial
    provider = Mock(return_value=None)
    with pytest.raises(ConnectionError):
        DeliverySnapshotEndpoint(room, ready=Mock(return_value=True), state=provider)
    assert participant.handlers == {}
    assert participant.unregistered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]
    if partial:
        with pytest.raises(rtc.RpcError):
            await participant.last_handler(SimpleNamespace(caller_identity="human", payload=""))
    provider.assert_not_called()


@pytest.mark.parametrize("unregister_error", [False, True])
async def test_shared_endpoint_close_is_single_owner_and_late_calls_fail(
    unregister_error: bool,
) -> None:
    """先关闭本地授权再注销，重复关闭或公开注销失败都不能恢复旧回调权限。"""
    participant, room = snapshot_room()
    provider = Mock(return_value=None)
    endpoint = DeliverySnapshotEndpoint(room, ready=Mock(return_value=True), state=provider)
    handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
    participant.unregister_error = unregister_error
    await asyncio.gather(endpoint.aclose(), endpoint.aclose())
    assert not endpoint.registered
    assert participant.unregistered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]
    with pytest.raises(rtc.RpcError):
        await handler(SimpleNamespace(caller_identity="human", payload=""))
    provider.assert_not_called()


async def test_shared_output_borrows_endpoint_and_network_failure_keeps_readiness() -> None:
    """整段完成的中性状态先排完，再隔离注入新消息失败；共享输出不能注销会话入口。"""
    participant, room = snapshot_room()

    def read_state() -> DeliveryState:
        """入口读取输出当前的不可变对象，测试不手工复制版本制造一致性。"""
        return output.snapshot

    endpoint = DeliverySnapshotEndpoint(room, ready=Mock(return_value=True), state=read_state)
    output = DeliveryTextOutput(room, snapshot_endpoint=endpoint)
    try:
        assert output.snapshot.instance == endpoint.instance
        assert participant.registered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]
        await output.bind_segment("shared-reply", DeliveryIntent("happy", "nod"))
        await output.capture_text("共享状态。")
        await output.drain()
        header = json.loads(participant.streams[0][0][DELIVERY_TOPIC])
        handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
        assert header == json.loads(await handler(SimpleNamespace(caller_identity="human")))
        output.flush()
        await output.drain()
        await output.finish_reply("shared-reply")
        await output.drain()
        participant.publish_failures = 1
        await output.bind_segment("failed-expression", DeliveryIntent("happy", "nod"))
        await output.capture_text("网络失败仍有正文。")
        await output.drain()
        assert output.snapshot.state == "closed"
        assert (
            json.loads(await handler(SimpleNamespace(caller_identity="human")))["state"] == "closed"
        )
        await output.aclose()
        assert endpoint.registered and participant.unregistered == []
        assert (
            json.loads(await handler(SimpleNamespace(caller_identity="human")))["state"] == "closed"
        )
    finally:
        await output.aclose()
        await endpoint.aclose()
    assert participant.unregistered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]


async def test_closed_shared_endpoint_rejects_unopened_bound_text() -> None:
    """共享入口撤销后不能新开有回复身份却无播放的字幕；无绑定普通字幕另有独立降级。"""
    participant, room = snapshot_room()
    endpoint = DeliverySnapshotEndpoint(
        room, ready=Mock(return_value=True), state=Mock(return_value=None)
    )
    output = DeliveryTextOutput(room, snapshot_endpoint=endpoint)
    await endpoint.aclose()
    try:
        await output.bind_segment("late", DeliveryIntent("happy", "wave"))
        await output.capture_text("仅保留正文。")
        await output.drain()
        assert participant.streams == []
        assert participant.packets == []
        assert participant.registered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]
    finally:
        await output.aclose()
    assert participant.unregistered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]


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
    """头与快照共用版本；字幕关闭保持播放许可，实际done才推进中性关闭版本。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("太好了，")
    await output.capture_text("我们继续。")
    await output.drain()
    attributes, writer = participant.streams[0]
    header = json.loads(attributes[DELIVERY_TOPIC])
    assert header == participant.packets[0] == json.loads(output.snapshot.to_json())
    assert header == {
        "v": 3,
        "instance": "test-instance",
        "revision": 1,
        "reply_id": "reply-1",
        "segment_id": attributes["lk.segment_id"],
        "state": "active",
        "style": "happy",
        "gesture": "none",
        "phase": "speaking",
        "turn_id": output.snapshot.turn_id,
        "gesture_id": "",
        "gesture_source": "none",
        "command_feedback": "none",
        "energy": 0.5,
    }
    assert "lk.expression" not in attributes
    assert "".join(writer.chunks) == "太好了，我们继续。"
    output.flush()
    await output.drain()
    assert json.loads(output.snapshot.to_json()) == header
    assert output.snapshot.phase == "speaking"
    await output.finish_reply("reply-1")
    assert output.snapshot.state == "closed"
    assert output.snapshot.revision == 3
    assert (output.snapshot.style, output.snapshot.gesture) == ("neutral", "none")
    assert writer.close_attributes == [{"lk.transcription_final": "true"}]


async def test_queued_segments_keep_immutable_metadata(delivery: tuple) -> None:
    """换段撤销旧身份但字幕flush不撤销新段，音频done后才产生最终关闭版本。"""
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
    await output.finish_reply("reply-1")
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
    """事件闸门验证取消顺序，使用生产默认I/O预算，避免共享20ms故障夹具把调度抖动混入此反例。"""
    output, participant, _ = delivery
    output._io_timeout = 1.0
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


async def test_user_turn_during_publish_discards_unopened_text_and_keeps_new_snapshot() -> None:
    """发送状态期间新开口立即换轮；尚未开放的旧正文必须丢弃，不能以关闭头补开旧流。"""
    participant, room = snapshot_room()
    presentation = PresentationController()

    def read_state() -> DeliveryState:
        """RPC 读取实时权威对象，让测试覆盖 await 前后状态真实替换而非人工拼装。"""
        return output.snapshot

    endpoint = DeliverySnapshotEndpoint(
        room, ready=Mock(return_value=True), state=read_state, instance="publish-race-instance"
    )
    output = DeliveryTextOutput(
        room, snapshot_endpoint=endpoint, presentation=presentation, io_timeout=1.0
    )
    try:
        presentation.complete_turn("上一个问题。")
        await output.drain()
        old_turn = presentation.turn_id
        participant.publish_gate = asyncio.Event()
        participant.publish_started = asyncio.Event()
        await output.bind_segment("old-reply", DeliveryIntent(), "旧回复正文。")
        await output.capture_text("旧回复正文。")
        await asyncio.wait_for(participant.publish_started.wait(), 0.5)
        old_speaking = output.snapshot
        assert old_speaking.phase == "speaking" and old_speaking.reply_id == "old-reply"
        presentation.phase("listening")
        listening = output.snapshot
        assert listening.phase == "listening" and listening.turn_id != old_turn
        assert listening.reply_id == listening.segment_id == listening.gesture_id == ""
        handler = participant.handlers[DELIVERY_SNAPSHOT_RPC]
        assert json.loads(await handler(SimpleNamespace(caller_identity="human"))) == json.loads(
            listening.to_json()
        )
        participant.publish_gate.set()
        await asyncio.wait_for(output.drain(), 0.5)
        assert participant.streams == []
        assert old_speaking.turn_id == old_turn and old_speaking.segment_id
        assert output.snapshot == listening
        assert json.loads(await handler(SimpleNamespace(caller_identity="human"))) == json.loads(
            listening.to_json()
        )
        assert all(
            packet["phase"] == "listening" and packet["reply_id"] == packet["segment_id"] == ""
            for packet in participant.packets
            if packet["turn_id"] == listening.turn_id
        )
    finally:
        participant.publish_gate.set()
        await output.aclose()
        await presentation.aclose()
        await endpoint.aclose()


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


async def test_state_publish_failure_discards_bound_text_and_next_segment_recovers(
    delivery: tuple,
) -> None:
    """许可发布失败不能冒充有播放的正文开流；单段失败不能阻塞下一段有效字幕。"""
    output, participant, _ = delivery
    participant.publish_failures = 1
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("正文仍然可见。")
    await output.drain()
    assert participant.streams == []
    assert output.snapshot.state == "closed"
    await output.bind_segment("reply-2", DeliveryIntent("gentle"), "恢复段。")
    await output.capture_text("恢复段。")
    await output.drain()
    attributes, writer = participant.streams[0]
    assert writer.chunks == ["恢复段。"]
    assert json.loads(attributes[DELIVERY_TOPIC])["state"] == "active"
    assert "lk.expression" not in attributes


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
    """保留原有限网络预算和非阻塞输入；超时未获许可不新开绑定流，后续仍可恢复。"""
    output, participant, _ = delivery
    participant.publish_gate = asyncio.Event()
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("超时仍显示字幕。")
    assert participant.attempts == []
    await asyncio.wait_for(output.drain(), timeout=0.5)
    assert output.snapshot.state == "closed"
    assert participant.streams == []
    participant.publish_gate = None
    await output.bind_segment("reply-2", DeliveryIntent("gentle"), "恢复段。")
    await output.capture_text("恢复段。")
    await output.drain()
    assert participant.streams[0][1].chunks == ["恢复段。"]
    assert json.loads(participant.streams[0][0][DELIVERY_TOPIC])["state"] == "active"


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
    assert participant.unregistered == []
    assert DELIVERY_SNAPSHOT_RPC in participant.handlers
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
    assert DELIVERY_SNAPSHOT_RPC in participant.handlers


async def test_shutdown_during_flush_still_closes_the_owned_writer(delivery: tuple) -> None:
    """字幕关闭的网络await若被关机取消，writer仍可由关机重收且播放许可立即关闭。"""
    output, participant, _ = delivery
    await output.bind_segment("reply-1", DeliveryIntent("happy", "nod"))
    await output.capture_text("已发送。")
    await output.drain()
    writer = participant.streams[0][1]
    started, release = asyncio.Event(), asyncio.Event()
    original_close = writer.aclose

    async def first_close_blocks(*, attributes: dict[str, str]) -> None:
        """只阻塞第一次网络尝试，取消尚未完成的请求不能冒称writer已经完成。"""
        if not started.is_set():
            started.set()
            await release.wait()
        await original_close(attributes=attributes)

    writer.aclose = first_close_blocks
    output.flush()
    await started.wait()
    await asyncio.wait_for(output.aclose(), timeout=0.5)
    assert writer.close_attributes == [{"lk.transcription_final": "true"}]
    assert output.snapshot.state == "closed"


async def test_expired_queued_segment_never_opens_after_new_binding(delivery: tuple) -> None:
    """尚未开放的旧段不能补开无音频字幕，下一段仍须携带自己的不可变许可。"""
    output, participant, _ = delivery
    await output.bind_segment("old", DeliveryIntent("happy", "wave"))
    await output.capture_text("上一段已经结束。")
    output.flush()
    await output.bind_segment("new", DeliveryIntent("gentle", "none"))
    await output.capture_text("正在播放的回复。")
    await output.drain()
    assert len(participant.streams) == 1
    attributes, writer = participant.streams[0]
    header = json.loads(attributes[DELIVERY_TOPIC])
    assert header["reply_id"] == "new" and header["segment_id"] == attributes["lk.segment_id"]
    assert header["state"] == "active"
    assert writer.chunks == ["正在播放的回复。"]
    assert not any(
        packet["reply_id"] == "old" and packet["state"] == "active"
        for packet in participant.packets
    )
    assert output.snapshot.reply_id == "new"
    assert output.snapshot.state == "active"


async def test_real_sdk_synchronizer_waits_for_playback_and_rotates_bound_segments(
    delivery: tuple,
) -> None:
    """真实同步器屏蔽播放前字幕，实际音频完成另触发done，不借字幕flush假装结束。"""
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
            await output.finish_reply(reply_id)
            attributes, writer = participant.streams[-1]
            assert json.loads(attributes[DELIVERY_TOPIC])["reply_id"] == reply_id
            assert "".join(writer.chunks) == text
            assert output.snapshot.phase == "idle"
            assert output.snapshot.state == "closed"
        ids = [attributes["lk.segment_id"] for attributes, _ in participant.streams]
        assert len(set(ids)) == 2
        assert len(audio.frames) == 2
    finally:
        await synchronizer.aclose()


async def test_late_normal_done_and_cancel_after_flush_respect_reply_identity(
    delivery: tuple,
) -> None:
    """字幕已结束仍必须允许精确打断，旧正常完成事件不能收掉下一回复或复活旧Jev。"""
    output, participant, _ = delivery
    await output.bind_segment("old", DeliveryIntent(), "旧回复")
    await output.capture_text("旧回复")
    await output.drain()
    output.flush()
    await output.drain()
    await output.bind_segment("new", DeliveryIntent(), "新回复")
    await output.capture_text("新回复")
    await output.drain()
    current = output.snapshot
    await output.finish_reply("old")
    assert output.snapshot == current
    output.flush()
    await output.drain()
    await output.cancel_reply("new")
    assert output.snapshot.phase == "idle"
    assert participant.streams[-1][1].close_attributes == [{"lk.transcription_final": "true"}]


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


async def test_text_flush_during_last_write_keeps_actual_playback_binding(delivery: tuple) -> None:
    """最后正文的网络await与字幕完成可交叉，合法播放不得被误作取消或拒绝迟到Jev。"""
    output, participant, _ = delivery
    await output.bind_segment("short-reply", DeliveryIntent(), "二")
    await output.capture_text("二")
    await output.drain()
    before = output.snapshot
    writer = participant.streams[0][1]
    started, release = asyncio.Event(), asyncio.Event()
    original_write = writer.write

    async def final_write(text: str) -> None:
        """只控制网络完成顺序，避免用真实延迟猜测flush与最后一字的竞争。"""
        started.set()
        await release.wait()
        await original_write(text)

    writer.write = final_write
    await output.capture_text("。")
    await started.wait()
    output.flush()
    release.set()
    await output.drain()
    assert output.snapshot == before
    assert writer.chunks == ["二", "。"]
    assert writer.close_attributes == [{"lk.transcription_final": "true"}]
    cue = PresentationCue(
        phase="speaking",
        turn_id=before.turn_id,
        reply_id=before.reply_id,
        segment_id=before.segment_id,
        intent=DeliveryIntent("happy", "nod"),
        gesture_id=f"{before.reply_id}:{before.segment_id}:nod",
        gesture_source="dialogue",
        energy=0.6,
    )
    output.present(cue)
    await output.drain()
    assert output.snapshot.gesture_id == cue.gesture_id
    await output.finish_reply("short-reply")
    closed = output.snapshot
    assert closed.phase == "idle" and closed.state == "closed"
    output.present(cue)
    await output.drain()
    assert output.snapshot == closed


async def test_real_sdk_single_word_flush_precedes_playout_completion(delivery: tuple) -> None:
    """真实同步器可以在音频完成前结束单字字幕；许可必须等公开播放完成边界再关闭。"""
    output, participant, _ = delivery
    audio = MemoryAudioOutput()
    synchronizer = TranscriptSynchronizer(next_in_chain_audio=audio, next_in_chain_text=output)
    frame = rtc.AudioFrame.create(sample_rate=24000, num_channels=1, samples_per_channel=16800)
    playing = None
    try:
        await output.bind_segment("single-word", DeliveryIntent(), "二")
        await synchronizer.text_output.capture_text("二")
        synchronizer.text_output.flush()
        await synchronizer.audio_output.capture_frame(frame)
        synchronizer.audio_output.flush()
        playing = asyncio.create_task(audio.wait_for_playout())
        audio.on_playback_started(created_at=time.time())
        await asyncio.wait_for(participant.stream_started.wait(), 1.0)
        writer = participant.streams[0][1]
        async with asyncio.timeout(1.0):
            while not writer.close_attributes:
                await asyncio.sleep(0.005)
        assert not playing.done()
        assert output.snapshot.phase == "speaking"
        audio.on_playback_finished(playback_position=frame.duration, interrupted=False)
        await playing
        await synchronizer.barrier()
        await output.finish_reply("single-word")
        assert output.snapshot.phase == "idle"
    finally:
        if playing is not None and not playing.done():
            playing.cancel()
            await asyncio.gather(playing, return_exceptions=True)
        await synchronizer.aclose()


async def test_single_word_flush_during_active_publish_preserves_subtitle_permission(
    delivery: tuple,
) -> None:
    """一字文本结束可发生在开流通知await期间，正常flush不得把合法字幕头降成closed。"""
    output, participant, _ = delivery
    participant.publish_gate = asyncio.Event()
    await output.bind_segment("short", DeliveryIntent(), "二")
    await output.capture_text("二")
    await participant.publish_started.wait()
    output.flush()
    participant.publish_gate.set()
    await output.drain()
    header = json.loads(participant.streams[0][0][DELIVERY_TOPIC])
    assert header["state"] == "active" and header["phase"] == "speaking"
    assert output.snapshot.reply_id == "short" and output.snapshot.phase == "speaking"
    await output.finish_reply("short")
    assert output.snapshot.phase == "idle"


async def test_thinking_style_update_preserves_same_turn_pending_playback(delivery: tuple) -> None:
    """Jev预判可先于字幕开流返回，同轮思考表情不得误删已绑定却尚未授权的播放候选。"""
    output, participant, _ = delivery
    await output.bind_segment("pending", DeliveryIntent(), "二")
    output.present(
        PresentationCue(
            phase="thinking",
            turn_id=output._presentation.turn_id,
            intent=DeliveryIntent("gentle"),
        )
    )
    await output.capture_text("二")
    output.flush()
    await output.drain()
    header = json.loads(participant.streams[0][0][DELIVERY_TOPIC])
    assert header["state"] == "active" and header["reply_id"] == "pending"
