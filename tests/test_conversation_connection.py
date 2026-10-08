"""用可控房间事件覆盖挂断认证与恢复竞态，不连接真实房间或模型。"""

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from livekit import rtc

from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure import conversation_connection
from xiaoya.infrastructure import livekit_conversation as adapter_module
from xiaoya.infrastructure.conversation_connection import ConversationConnection


class MemoryRoom:
    """仅实现 SDK 公开事件与参与者字典，禁止测试借内部 RoomIO 状态走捷径。"""

    def __init__(self) -> None:
        """公开 metadata 为空即未知客户端，传输和 RPC 仍完全限定在内存边界。"""
        self.metadata = ""
        self.owner = SimpleNamespace(identity="owner", sid="PA_owner_1")
        self.remote_participants = {self.owner.identity: self.owner}
        self.local_participant = SimpleNamespace(
            identity="agent",
            sid="PA_agent",
            publish_data=AsyncMock(),
            register_rpc_method=Mock(),
            unregister_rpc_method=Mock(),
        )
        self.connection_state = rtc.ConnectionState.CONN_CONNECTED
        self.handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, callback: Callable) -> None:
        """回调顺序与真实事件发射器一致，注册不会隐式重放已经发生的事件。"""
        self.handlers.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable) -> None:
        """保留空列表便于断言所有订阅均已释放。"""
        self.handlers[event].remove(callback)

    def emit(self, event: str, value: object) -> None:
        """复制监听列表以支持回调中取消订阅，模拟 SDK 的同步事件通知。"""
        for callback in tuple(self.handlers.get(event, ())):
            callback(value)

    def leave(self, participant: SimpleNamespace) -> None:
        """先更新房间事实再发事件；迟到旧 SID 事件不能删除新连接。"""
        current = self.remote_participants.get(participant.identity)
        if current is not None and current.sid == participant.sid:
            self.remote_participants.pop(participant.identity)
        self.emit("participant_disconnected", participant)

    def join(self, participant: SimpleNamespace) -> None:
        """同身份的新 SID 是合法恢复，不额外创建业务会话。"""
        self.remote_participants[participant.identity] = participant
        self.emit("participant_connected", participant)


def end_packet(room: MemoryRoom, **changes: object) -> rtc.DataPacket:
    """默认包完全符合固定协议，异常用例只替换本次需要验证的字段。"""
    payload = {
        "v": 1,
        "type": "user_end",
        "request_id": str(uuid4()),
        "target_agent_sid": room.local_participant.sid,
    }
    payload.update(changes)
    return rtc.DataPacket(
        data=json.dumps(payload).encode(),
        participant=room.remote_participants.get("owner"),
        topic="xiaoya.delivery",
        kind=rtc.DataPacketKind.KIND_RELIABLE,
    )


@pytest.fixture
async def boundary():
    """大多数用例从真实绑定完成的状态开始，结束时统一检查任务不会外溢。"""
    room = MemoryRoom()
    on_end = Mock()
    connection = ConversationConnection(room, on_end)
    connection.listen()
    room_io = SimpleNamespace(wait_for_ready=AsyncMock(), linked_participant=room.owner)
    await connection.bind(room_io)
    try:
        yield SimpleNamespace(room=room, connection=connection, on_end=on_end, room_io=room_io)
    finally:
        await connection.aclose()


async def test_same_identity_rejoin_cancels_one_deadline_and_rejects_late_events(boundary):
    """新 SID 接管恢复与只读授权，重复离场不续期，旧事件既不能误关也不能撤销新授权。"""
    room, connection = boundary.room, boundary.connection
    assert connection.is_owner("owner")
    room.leave(room.owner)
    assert not connection.is_owner("owner")
    timer = connection._recovery_timer
    generation = connection._generation
    assert 29 < timer.when() - asyncio.get_running_loop().time() <= 30
    room.leave(room.owner)
    assert connection._recovery_timer is timer
    room.join(SimpleNamespace(identity="other", sid="PA_other"))
    assert connection._recovery_timer is timer
    replacement = SimpleNamespace(identity="owner", sid="PA_owner_2")
    room.join(replacement)
    assert connection.is_owner("owner")
    assert timer.cancelled() and connection._recovery_timer is None
    room.leave(room.owner)
    assert connection.is_owner("owner")
    connection._recovery_expired(generation)
    boundary.on_end.assert_not_called()
    room.leave(replacement)
    connection._recovery_expired(generation)
    boundary.on_end.assert_not_called()
    connection._recovery_expired(connection._generation)
    connection._recovery_expired(connection._generation)
    boundary.on_end.assert_called_once_with()


async def test_current_room_membership_wins_over_late_timer(boundary):
    """重新加入的房间事实已更新但事件尚未送达时，也不能按旧快照结束。"""
    room, connection = boundary.room, boundary.connection
    room.leave(room.owner)
    generation = connection._generation
    room.remote_participants["owner"] = SimpleNamespace(identity="owner", sid="PA_owner_2")
    connection._recovery_expired(generation)
    boundary.on_end.assert_not_called()
    assert connection._owner_sid == "PA_owner_2"


async def test_close_invalidates_a_callback_already_queued(boundary):
    """取消句柄不能撤回已入队回调，所以关闭后的代次和状态仍必须阻止迟到结束。"""
    boundary.room.leave(boundary.room.owner)
    timer = boundary.connection._recovery_timer
    generation = boundary.connection._generation
    await boundary.connection.aclose()
    assert timer.cancelled()
    boundary.connection._recovery_expired(generation)
    boundary.on_end.assert_not_called()
    assert not any(boundary.room.handlers.values())


async def test_real_room_disconnect_is_left_to_the_job(boundary):
    """真实房间终态已经触发 SDK Job 回收，恢复计时器不再发反向终止通知。"""
    boundary.room.leave(boundary.room.owner)
    boundary.room.connection_state = rtc.ConnectionState.CONN_DISCONNECTED
    boundary.connection._recovery_expired(boundary.connection._generation)
    boundary.on_end.assert_not_called()


async def test_explicit_hangup_acknowledges_once_and_is_not_a_dialogue(boundary):
    """接受挂断即关闭只读就绪授权，确认仍先于终止通知且重复请求不能重复发布。"""
    packet = end_packet(boundary.room)
    boundary.room.emit("data_received", packet)
    assert not boundary.connection.is_owner("owner")
    boundary.room.emit("data_received", packet)
    boundary.room.emit("data_received", end_packet(boundary.room))
    boundary.on_end.assert_not_called()
    await boundary.connection._ack_task
    publish = boundary.room.local_participant.publish_data
    publish.assert_awaited_once()
    data = publish.call_args.args[0]
    assert len(data.encode()) <= 512
    assert json.loads(data) == {
        "v": 1,
        "type": "user_end_ack",
        "request_id": json.loads(packet.data)["request_id"],
        "agent_sid": "PA_agent",
    }
    assert publish.call_args.kwargs == {
        "reliable": True,
        "topic": "xiaoya.delivery",
        "destination_identities": ["owner"],
    }
    boundary.on_end.assert_called_once_with()


@pytest.mark.parametrize(
    "changes",
    [
        {"v": True},
        {"v": 2},
        {"v": "1"},
        {"type": "user_end_ack"},
        {"target_agent_sid": "PA_old_agent"},
        {"target_agent_sid": None},
        {"request_id": "not-a-uuid"},
        {"request_id": "E672D081-62E5-457B-9B0F-09279E2024B9"},
        {"request_id": 123},
        {"identity": "owner"},
    ],
)
async def test_invalid_control_schema_cannot_end_a_conversation(boundary, changes):
    """版本、目标实例与规范 UUID 均精确验证，不把近似或扩展控制包当作挂断。"""
    boundary.room.emit("data_received", end_packet(boundary.room, **changes))
    await asyncio.sleep(0)
    boundary.on_end.assert_not_called()
    boundary.room.local_participant.publish_data.assert_not_awaited()


@pytest.mark.parametrize("payload", [b"{", b"[]", b"null", b"\xff", b" " * 513])
async def test_malformed_or_oversized_bytes_are_ignored(boundary, payload):
    """解析异常不能逃出 SDK 同步回调，体积限制在解码之前生效。"""
    packet = end_packet(boundary.room)
    packet.data = payload
    boundary.room.emit("data_received", packet)
    await asyncio.sleep(0)
    boundary.on_end.assert_not_called()


@pytest.mark.parametrize("invalid", ["topic", "lossy", "no_sender", "other", "left", "old_sid"])
async def test_transport_sender_and_presence_are_required(boundary, invalid):
    """正文无法冒充绑定身份；房内不存在或 SID 已过期的来源即使格式正确也无权结束。"""
    room = boundary.room
    packet = end_packet(room)
    if invalid == "topic":
        packet.topic = "lk.chat"
    elif invalid == "lossy":
        packet.kind = rtc.DataPacketKind.KIND_LOSSY
    elif invalid == "no_sender":
        packet.participant = None
    elif invalid == "other":
        packet.participant = SimpleNamespace(identity="other", sid="PA_other")
        room.join(packet.participant)
    elif invalid == "left":
        room.remote_participants.clear()
    else:
        room.join(SimpleNamespace(identity="owner", sid="PA_owner_2"))
    room.emit("data_received", packet)
    await asyncio.sleep(0)
    boundary.on_end.assert_not_called()
    room.local_participant.publish_data.assert_not_awaited()


async def test_rejoined_owner_can_explicitly_end(boundary):
    """认证 SID 随可信的同身份加入更新，合法恢复后不丢失快速挂断能力。"""
    boundary.room.leave(boundary.room.owner)
    boundary.room.join(SimpleNamespace(identity="owner", sid="PA_owner_2"))
    boundary.room.emit("data_received", end_packet(boundary.room))
    await boundary.connection._ack_task
    boundary.on_end.assert_called_once_with()


@pytest.mark.parametrize("failure", ["error", "timeout", "reconnecting"])
async def test_ack_failure_still_requests_cleanup(boundary, monkeypatch, failure):
    """确认失败只影响快路径，不会把用户已明确结束的会话永久留在后台。"""
    cancelled = asyncio.Event()

    async def stalled_publish(*args, **kwargs):
        """模拟永不返回的网络请求，并验证有界超时确实取消它。"""
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if failure == "error":
        boundary.room.local_participant.publish_data.side_effect = RuntimeError("断网")
    elif failure == "timeout":
        monkeypatch.setattr(conversation_connection, "ACK_TIMEOUT", 0.001)
        boundary.room.local_participant.publish_data.side_effect = stalled_publish
    else:
        boundary.room.connection_state = rtc.ConnectionState.CONN_RECONNECTING
    boundary.room.emit("data_received", end_packet(boundary.room))
    await boundary.connection._ack_task
    boundary.on_end.assert_called_once_with()
    if failure == "timeout":
        assert cancelled.is_set()
    elif failure == "reconnecting":
        boundary.room.local_participant.publish_data.assert_not_awaited()


async def test_close_cancels_ack_and_ignores_captured_callbacks(boundary):
    """Job 已开始回收时撤销确认任务，迟到事件不得再次请求终止或注册新计时器。"""
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled_publish(*args, **kwargs):
        """将确认停在实际发送等待中，保证测试覆盖取消而非已完成任务。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    boundary.room.local_participant.publish_data.side_effect = stalled_publish
    callback = boundary.room.handlers["data_received"][0]
    callback(end_packet(boundary.room))
    await entered.wait()
    await boundary.connection.aclose()
    await boundary.connection.aclose()
    assert cancelled.is_set()
    assert not any(boundary.room.handlers.values())
    callback(end_packet(boundary.room))
    boundary.connection._on_disconnected(boundary.room.owner)
    assert boundary.connection._recovery_timer is None
    boundary.on_end.assert_not_called()


async def test_ready_wait_is_registered_first_and_cancelled_only_on_close():
    """首次绑定及取消期间均不授权，调用者取消不破坏 SDK 等待，统一关闭才回收。"""
    room, entered, cancelled = MemoryRoom(), asyncio.Event(), asyncio.Event()

    async def ready():
        """在公开 readiness 边界阻塞，避免测试触碰 SDK 的内部 future。"""
        assert set(room.handlers) == {
            "participant_connected",
            "participant_disconnected",
            "data_received",
        }
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    connection = ConversationConnection(room, Mock())
    assert not connection.is_owner("owner")
    connection.listen()
    connection.listen()
    room_io = SimpleNamespace(wait_for_ready=ready, linked_participant=room.owner)
    waiter = asyncio.create_task(connection.bind(room_io))
    await entered.wait()
    assert not connection.is_owner("owner")
    room.emit("data_received", end_packet(room))
    assert connection._ack_task is None
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not connection.is_owner("owner")
    assert not cancelled.is_set()
    await connection.aclose()
    assert cancelled.is_set()
    assert not any(room.handlers.values())


@pytest.mark.parametrize("invalid", ["ready_error", "no_owner", "empty_identity"])
async def test_owner_binding_failure_does_not_guess_a_participant(invalid):
    """首次就绪失败不开放快照或控制认证，不能凭任意房内用户绕过 SDK 已确认的绑定。"""
    room = MemoryRoom()
    connection = ConversationConnection(room, Mock())
    connection.listen()
    room_io = SimpleNamespace(wait_for_ready=AsyncMock(), linked_participant=room.owner)
    if invalid == "ready_error":
        room_io.wait_for_ready.side_effect = RuntimeError("ready 失败")
    elif invalid == "no_owner":
        room_io.linked_participant = None
    else:
        room_io.linked_participant = SimpleNamespace(identity="", sid="PA_empty")
    try:
        with pytest.raises(RuntimeError):
            await connection.bind(room_io)
        assert not connection.is_owner("owner")
        room.emit("data_received", end_packet(room))
        assert connection._ack_task is None
    finally:
        await connection.aclose()


async def test_owner_departure_during_binding_has_a_bounded_deadline():
    """就绪返回到读取房内事实之间已离场时仍有清理期限，而不是等待下一次离场事件。"""
    room = MemoryRoom()
    on_end = Mock()
    connection = ConversationConnection(room, on_end)
    connection.listen()
    room.leave(room.owner)
    room_io = SimpleNamespace(wait_for_ready=AsyncMock(), linked_participant=room.owner)
    await connection.bind(room_io)
    assert connection._recovery_timer is not None
    connection._recovery_expired(connection._generation)
    on_end.assert_called_once_with()
    await connection.aclose()


async def test_confirmed_owner_survives_first_ready_disconnect_race():
    """SDK 清空就绪时已确认过的绑定不会改成陌生用户，也不会立即结束可恢复会话。"""
    room, on_end = MemoryRoom(), Mock()
    room_io = SimpleNamespace(linked_participant=room.owner)

    async def ready_then_disconnect():
        """在同一调度轮次模拟首次就绪后立刻清空绑定，复现 SDK 的公开事件顺序。"""
        room.leave(room.owner)
        room_io.linked_participant = None

    room_io.wait_for_ready = ready_then_disconnect
    connection = ConversationConnection(room, on_end)
    connection.listen()
    await connection.bind(room_io)
    assert connection._recovery_timer is not None
    room.join(SimpleNamespace(identity="owner", sid="PA_owner_2"))
    assert connection._recovery_timer is None
    on_end.assert_not_called()
    await connection.aclose()


async def test_readiness_that_never_settles_is_a_bounded_startup_failure(monkeypatch):
    """SDK 后台输出初始化失败可能不完成 ready future，启动不能因此永久悬挂。"""
    room, cancelled = MemoryRoom(), asyncio.Event()

    async def never_ready():
        """保留一个无结果的公开等待，关闭时确认它确实被取消回收。"""
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(conversation_connection, "RECOVERY_TIMEOUT", 0.001)
    connection = ConversationConnection(room, Mock())
    connection.listen()
    room_io = SimpleNamespace(wait_for_ready=never_ready, linked_participant=room.owner)
    with pytest.raises(RuntimeError, match="初始化超时"):
        await connection.bind(room_io)
    assert not cancelled.is_set()
    await connection.aclose()
    assert cancelled.is_set()


async def test_listener_failure_does_not_skip_other_unsubscriptions(monkeypatch):
    """注销单个回调失败也要尝试其他回调，不能把故障路径变成长驻的事件泄漏。"""
    room = MemoryRoom()
    connection = ConversationConnection(room, Mock())
    connection.listen()
    original_off = room.off
    attempted = []

    def failing_off(event, callback):
        """删除真实监听后模拟失败，验证栈清理继续执行而非遇错跳出。"""
        attempted.append(event)
        original_off(event, callback)
        if event == "data_received":
            raise RuntimeError("注销失败")

    monkeypatch.setattr(room, "off", failing_off)
    with pytest.raises(RuntimeError, match="注销失败"):
        await connection.aclose()
    assert set(attempted) == {"participant_connected", "participant_disconnected", "data_received"}
    assert not any(room.handlers.values())


async def test_real_connection_protocol_closes_single_expressive_adapter(
    monkeypatch, private_settings
):
    """同一连接认证和快照登记贯通唯一语音链，结束清理必须注销端点且不留监听。"""
    room, on_terminal = MemoryRoom(), Mock()
    session = Mock(
        start=AsyncMock(),
        aclose=AsyncMock(),
        room_io=SimpleNamespace(wait_for_ready=AsyncMock(), linked_participant=room.owner),
    )
    monkeypatch.setattr(adapter_module, "AgentSession", Mock(return_value=session))
    monkeypatch.setattr(adapter_module.inference, "TurnDetector", Mock())
    monkeypatch.setattr(
        adapter_module, "LocalStreamingSTT", Mock(return_value=Mock(aclose=AsyncMock()))
    )
    for name in ("LLM", "TTS"):
        monkeypatch.setattr(
            adapter_module.openai, name, Mock(side_effect=lambda **kwargs: Mock(aclose=AsyncMock()))
        )
    agent = Mock(enable_delivery=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(adapter_module, "ExpressiveAgent", Mock(return_value=agent))
    conversation = adapter_module.LiveKitVoiceConversation(
        room=room,
        settings=private_settings,
        vad=Mock(),
        on_terminal=on_terminal,
    )
    await conversation.start(AssistantProfile())
    assert session.start.call_args.kwargs["room_options"].close_on_disconnect is False
    room.emit("data_received", end_packet(room))
    await conversation._connection._ack_task
    await conversation.close()
    session.aclose.assert_awaited_once()
    on_terminal.assert_called_once_with()
    assert not any(room.handlers.values())
    room.local_participant.register_rpc_method.assert_called_once()
    room.local_participant.unregister_rpc_method.assert_called_once_with(
        "xiaoya.getDeliverySnapshot"
    )


async def test_owner_query_requires_confirmed_identity_and_current_sid(boundary):
    """新成员已进房间表但连接回调尚未接管时，不从 identity 相同推断已完成恢复。"""
    room, connection = boundary.room, boundary.connection
    assert connection.is_owner("owner")
    assert not connection.is_owner("")
    room.join(SimpleNamespace(identity="other", sid="PA_other"))
    assert not connection.is_owner("other")
    replacement = SimpleNamespace(identity="owner", sid="PA_owner_2")
    room.remote_participants["owner"] = replacement
    assert not connection.is_owner("owner")
    room.emit("participant_connected", replacement)
    assert connection.is_owner("owner")
    room.remote_participants.clear()
    assert not connection.is_owner("owner")


async def test_owner_query_is_closed_before_resource_wait_finishes(boundary):
    """关闭是即时授权边界；即使 SDK 字典仍保留用户，也不能在回收等待期间提供 ready。"""
    await boundary.connection.aclose()
    assert "owner" in boundary.room.remote_participants
    assert not boundary.connection.is_owner("owner")


async def test_owner_query_opens_only_after_successful_binding():
    """有真实房内身份仍需等待 SDK 首次 readiness，避免初始化中的端点提前启用用户输入。"""
    room, entered, release = MemoryRoom(), asyncio.Event(), asyncio.Event()
    connection = ConversationConnection(room, Mock())
    connection.listen()

    async def delayed_ready():
        """只延后公开绑定边界，不接触 SDK 私有 future 或依赖计时器凑时序。"""
        entered.set()
        await release.wait()

    waiter = asyncio.create_task(
        connection.bind(
            SimpleNamespace(wait_for_ready=delayed_ready, linked_participant=room.owner)
        )
    )
    try:
        await entered.wait()
        assert not connection.is_owner("owner")
        release.set()
        await waiter
        assert connection.is_owner("owner")
    finally:
        release.set()
        await waiter
        await connection.aclose()
