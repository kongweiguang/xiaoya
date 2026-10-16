"""公开回执必须通过当前连接授权，错误与部分注册不能泄漏动作完成事实。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from livekit import rtc
from test_delivery_output import snapshot_room

from xiaoya.infrastructure.delivery_output import (
    DELIVERY_SNAPSHOT_RPC,
    PRESENTATION_ACK_RPC,
    DeliverySnapshotEndpoint,
)


def invocation(endpoint, *, caller="human", **changes):
    """传输身份与消息身份分别构造，覆盖仅改正文不能越权的真实RPC约束。"""
    body = dict(instance=endpoint.instance, turn_id="turn", gesture_id="action", status="started")
    body.update(changes)
    return SimpleNamespace(caller_identity=caller, payload=json.dumps(body))


async def test_ack_uses_ready_owner_and_never_reads_model_output():
    """原四字段缺少stroke事实时传None，仍委托身份验证且不读取模型正文。"""
    participant, room = snapshot_room()
    ready, acknowledged = Mock(return_value=True), Mock(return_value=True)
    endpoint = DeliverySnapshotEndpoint(
        room, ready=ready, state=Mock(return_value=None), acknowledge=acknowledged
    )
    try:
        handler = participant.handlers[PRESENTATION_ACK_RPC]
        assert json.loads(await handler(invocation(endpoint))) == {"accepted": True}
        acknowledged.assert_called_once_with("turn", "action", "started", None)
        ready.return_value = False
        with pytest.raises(rtc.RpcError) as denied:
            await handler(invocation(endpoint))
        assert denied.value.code == 2001
        assert acknowledged.call_count == 1
        await endpoint.aclose()
        with pytest.raises(rtc.RpcError):
            await handler(invocation(endpoint))
    finally:
        await endpoint.aclose()


@pytest.mark.parametrize(
    ("status", "stroke_started"),
    [
        ("started", True),
        ("completed", True),
        ("canceled", False),
        ("canceled", True),
        ("unavailable", False),
    ],
)
async def test_ack_forwards_only_explicit_boolean_stroke_fact(status, stroke_started):
    """同一公开RPC只透传经严格校验的事实，能否释放预算仍由应用已offer账本决定。"""
    participant, room = snapshot_room()
    callback = Mock(return_value=True)
    endpoint = DeliverySnapshotEndpoint(
        room, ready=Mock(return_value=True), state=Mock(return_value=None), acknowledge=callback
    )
    try:
        data = invocation(endpoint, status=status, stroke_started=stroke_started)
        assert json.loads(await participant.handlers[PRESENTATION_ACK_RPC](data)) == {
            "accepted": True
        }
        callback.assert_called_once_with("turn", "action", status, stroke_started)
    finally:
        await endpoint.aclose()


@pytest.mark.parametrize(
    "changes",
    [
        {"stroke_started": None},
        {"stroke_started": 0},
        {"stroke_started": 1},
        {"stroke_started": "false"},
        {"stroke_started": []},
        {"stroke_started": {}},
        {"stroke_started": False},
        {"status": "completed", "stroke_started": False},
        {"status": "canceled", "stroke_started": False, "other": True},
    ],
)
async def test_ack_rejects_invalid_or_contradictory_stroke_fact(changes):
    """布尔数值混淆、明确发力状态却false和额外字段均须在进入应用前拒绝。"""
    participant, room = snapshot_room()
    callback = Mock(return_value=True)
    endpoint = DeliverySnapshotEndpoint(
        room, ready=Mock(return_value=True), state=Mock(return_value=None), acknowledge=callback
    )
    try:
        with pytest.raises(rtc.RpcError) as rejected:
            await participant.handlers[PRESENTATION_ACK_RPC](invocation(endpoint, **changes))
        assert rejected.value.code == 2002
        callback.assert_not_called()
    finally:
        await endpoint.aclose()


@pytest.mark.parametrize("caller", ["", "outsider"])
async def test_payload_cannot_replace_transport_owner(caller):
    """正确动作ID并不授予权限，房间外调用者的回执不能到达应用控制器。"""
    participant, room = snapshot_room()
    callback = Mock(return_value=True)
    endpoint = DeliverySnapshotEndpoint(
        room, ready=Mock(return_value=True), state=Mock(return_value=None), acknowledge=callback
    )
    try:
        with pytest.raises(rtc.RpcError):
            await participant.handlers[PRESENTATION_ACK_RPC](invocation(endpoint, caller=caller))
        callback.assert_not_called()
    finally:
        await endpoint.aclose()


@pytest.mark.parametrize(
    "changes", [{"instance": "old"}, {"status": "success"}, {"turn_id": ""}, {"gesture_id": 7}]
)
async def test_malformed_or_old_ack_is_rejected_before_application(changes):
    """旧会话和无效终态必须显式拒绝，不能用RPC返回成功伪造表现完成。"""
    participant, room = snapshot_room()
    callback = Mock(return_value=True)
    endpoint = DeliverySnapshotEndpoint(
        room, ready=Mock(return_value=True), state=Mock(return_value=None), acknowledge=callback
    )
    try:
        with pytest.raises(rtc.RpcError) as rejected:
            await participant.handlers[PRESENTATION_ACK_RPC](invocation(endpoint, **changes))
        assert rejected.value.code == 2002
        callback.assert_not_called()
    finally:
        await endpoint.aclose()


async def test_second_registration_failure_revokes_both_captured_handlers():
    """快照和回执是一次装配，第二次注册失败时首个已安装回调也要立即失效。"""
    participant, room = snapshot_room()
    original = participant.register_rpc_method
    captured = []

    def fail_ack(name, handler):
        """模拟FFI安装后报错，不能仅注销方法而忽略捕获到的旧Python回调。"""
        original(name, handler)
        captured.append(handler)
        if name == PRESENTATION_ACK_RPC:
            raise ConnectionError("回执登记失败")

    participant.register_rpc_method = fail_ack
    with pytest.raises(ConnectionError):
        DeliverySnapshotEndpoint(room, ready=Mock(return_value=True), state=Mock(return_value=None))
    assert participant.unregistered == [DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC]
    assert participant.handlers == {}
    for handler in captured:
        with pytest.raises(rtc.RpcError):
            await handler(SimpleNamespace(caller_identity="human", payload="{}"))
