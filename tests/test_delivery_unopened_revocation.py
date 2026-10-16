"""真实输出队列的撤权竞态，不用观察器过滤或伪造已打开字幕来绕过旧流问题。"""

import asyncio
import json

import pytest
from test_delivery_output import delivery as delivery

from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.infrastructure.delivery_output import DELIVERY_TOPIC, DeliveryTextOutput


async def revoke_unopened(output: DeliveryTextOutput, reason: str) -> None:
    """只经已有生命周期入口撤许可，覆盖未调用cancel_reply的真实打断窗口。"""
    if reason == "new-turn":
        output._presentation.phase("listening")
    elif reason == "idle":
        output._presentation.phase("idle")
    elif reason == "new-segment":
        await output.bind_segment("new-reply", DeliveryIntent("gentle"), "新回复。")
    else:
        assert reason == "endpoint"
        await output._snapshot_endpoint.aclose()


@pytest.mark.parametrize("reason", ["new-turn", "idle", "new-segment", "endpoint"])
async def test_queued_unopened_bound_text_is_dropped_after_revocation(
    delivery: tuple, reason: str
) -> None:
    """同步器可在撤权后才排出旧词；没有已开流事实就不能再建立无音频旧字幕。"""
    output, participant, _ = delivery
    output._presentation.complete_turn("旧问题。")
    await output.drain()
    await output.bind_segment("old-reply", DeliveryIntent(), "旧回复。")
    await output.capture_text("旧回复。")
    await revoke_unopened(output, reason)
    await output.drain()
    assert participant.streams == []
    assert not any(
        packet["reply_id"] == "old-reply" and packet["phase"] == "speaking"
        for packet in participant.packets
    )


@pytest.mark.parametrize("reason", ["new-turn", "idle", "new-segment", "endpoint"])
async def test_publish_await_revocation_never_opens_old_stream_and_next_input_recovers(
    delivery: tuple, reason: str
) -> None:
    """发布许可期间仍能撤权，重核后丢旧流但不让下一合法句段永久失声。"""
    output, participant, _ = delivery
    output._io_timeout = 1.0
    output._presentation.complete_turn("旧问题。")
    await output.drain()
    participant.publish_gate = asyncio.Event()
    participant.publish_started = asyncio.Event()
    await output.bind_segment("old-reply", DeliveryIntent(), "旧回复。")
    await output.capture_text("旧回复。")
    await asyncio.wait_for(participant.publish_started.wait(), 0.5)
    assert output.snapshot.phase == "speaking"
    await revoke_unopened(output, reason)
    participant.publish_gate.set()
    await asyncio.wait_for(output.drain(), 0.5)
    assert participant.streams == []

    if reason != "endpoint":
        if reason != "new-segment":
            output._presentation.complete_turn("下一有效问题。")
            await output.bind_segment("new-reply", DeliveryIntent("gentle"), "新回复。")
        await output.capture_text("新回复。")
        output.flush()
        await output.drain()
        assert len(participant.streams) == 1
        attributes, writer = participant.streams[0]
        header = json.loads(attributes[DELIVERY_TOPIC])
        assert header["reply_id"] == "new-reply" and header["phase"] == "speaking"
        assert writer.chunks == ["新回复。"]
        assert writer.close_attributes == [{"lk.transcription_final": "true"}]


async def test_already_open_stream_can_finish_owned_text_after_turn_revocation(
    delivery: tuple,
) -> None:
    """已开放字幕的真实尾词仍按原流收尾，修复只阻止撤权后新建流而不截断旧writer。"""
    output, participant, _ = delivery
    output._presentation.complete_turn("旧问题。")
    await output.drain()
    await output.bind_segment("old-reply", DeliveryIntent(), "旧回复尾词。")
    await output.capture_text("旧回复")
    await output.drain()
    attributes, writer = participant.streams[0]
    old_header = json.loads(attributes[DELIVERY_TOPIC])
    output._presentation.phase("listening")
    current = output.snapshot
    assert current.turn_id != old_header["turn_id"]
    await output.capture_text("尾词。")
    output.flush()
    await output.drain()
    assert len(participant.streams) == 1
    assert writer.chunks == ["旧回复", "尾词。"]
    assert writer.close_attributes == [{"lk.transcription_final": "true"}]
    assert output.snapshot == current
