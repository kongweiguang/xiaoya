"""把暂时离场与明确挂断分开处理，保留 SDK 的同身份重连能力。"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import ExitStack
from uuid import UUID

from livekit import rtc
from livekit.agents.voice.room_io import RoomIO

DELIVERY_TOPIC = "xiaoya.delivery"
RECOVERY_TIMEOUT = 30.0
ACK_TIMEOUT = 0.5
MAX_CONTROL_BYTES = 512
logger = logging.getLogger(__name__)


class ConversationConnection:
    """仅接受 SDK 实际绑定的用户，重连不重建业务会话或重放工具。"""

    def __init__(self, room: rtc.Room, on_end: Callable[[], None]) -> None:
        """绑定与只读就绪授权属于当前 Job，初始化中的房内成员不能提前获得会话权限。"""
        self._room = room
        self._on_end = on_end
        self._owner_identity: str | None = None
        self._owner_sid: str | None = None
        self._owner_ready = False
        self._listening = False
        self._closed = False
        self._ending = False
        self._notified = False
        self._generation = 0
        self._recovery_timer: asyncio.TimerHandle | None = None
        self._ready_task: asyncio.Task[None] | None = None
        self._ack_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    def listen(self) -> None:
        """先订阅再等待首次绑定，避免初始化间隙遗漏离场或重连事件。"""
        if self._closed or self._listening:
            return
        self._listening = True
        self._room.on("participant_connected", self._on_connected)
        self._room.on("participant_disconnected", self._on_disconnected)
        self._room.on("data_received", self._on_data)

    async def bind(self, room_io: RoomIO) -> None:
        """完整绑定后才开放只读授权；取消调用者不连带取消 SDK，统一关闭时再回收。"""
        if self._closed:
            raise RuntimeError("语音会话在绑定参与者前已关闭")
        self._owner_ready = False
        initially_linked = room_io.linked_participant
        self._ready_task = asyncio.create_task(room_io.wait_for_ready())
        try:
            async with asyncio.timeout(RECOVERY_TIMEOUT):
                await asyncio.shield(self._ready_task)
        except TimeoutError as error:
            raise RuntimeError("等待语音通话初始化超时") from error
        if self._closed:
            raise RuntimeError("语音会话在绑定参与者前已关闭")
        # 首次就绪和离场可能同轮发生；只保留 SDK 确认过的身份，不猜房内其他用户。
        participant = room_io.linked_participant or initially_linked
        if participant is None or not participant.identity or not participant.sid:
            raise RuntimeError("语音会话未绑定有效参与者")
        self._owner_identity = participant.identity
        self._owner_sid = participant.sid
        current = self._room.remote_participants.get(self._owner_identity)
        if current is None:
            self._start_recovery()
        else:
            self._owner_sid = current.sid
        self._owner_ready = True

    def is_owner(self, identity: str) -> bool:
        """认证只读快照也核对当前 SID，成员广播尚未接管或会话正在结束时保持关门。"""
        if (
            not self._owner_ready
            or self._closed
            or self._ending
            or not identity
            or identity != self._owner_identity
            or not self._owner_sid
        ):
            return False
        current = self._room.remote_participants.get(identity)
        return (
            current is not None and current.identity == identity and current.sid == self._owner_sid
        )

    def _on_connected(self, participant: rtc.RemoteParticipant) -> None:
        """同身份的新 SID 接管恢复窗口，捕获过的旧回调不能影响后来的连接。"""
        if self._closed or self._ending or participant.identity != self._owner_identity:
            return
        current = self._room.remote_participants.get(participant.identity)
        if current is None or current.sid != participant.sid:
            return
        self._owner_sid = participant.sid
        self._cancel_recovery()

    def _on_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        """SDK 完整重连也可能报告主动离场，因此不把断开原因当作明确挂断。"""
        if (
            self._closed
            or self._ending
            or participant.identity != self._owner_identity
            or participant.sid != self._owner_sid
        ):
            return
        current = self._room.remote_participants.get(participant.identity)
        if current is not None:
            self._owner_sid = current.sid
            self._cancel_recovery()
            return
        self._start_recovery()

    def _start_recovery(self) -> None:
        """重复离场不延长期限，单个计时器与代次共同隔离已取消的回调。"""
        if self._recovery_timer is None:
            self._generation += 1
            self._recovery_timer = asyncio.get_running_loop().call_later(
                RECOVERY_TIMEOUT, self._recovery_expired, self._generation
            )

    def _cancel_recovery(self) -> None:
        """即使回调已经进入事件队列，递增代次仍使它无法关闭恢复后的用户。"""
        self._generation += 1
        if self._recovery_timer is not None:
            self._recovery_timer.cancel()
            self._recovery_timer = None

    def _recovery_expired(self, generation: int) -> None:
        """到期再核对房内事实，真正的房间断开仍由 SDK Job 生命周期负责。"""
        if self._closed or self._ending or generation != self._generation:
            return
        self._recovery_timer = None
        current = self._room.remote_participants.get(self._owner_identity)
        if current is not None:
            self._owner_sid = current.sid
            self._cancel_recovery()
        elif self._room.connection_state != rtc.ConnectionState.CONN_DISCONNECTED:
            self._notify_end()

    def _on_data(self, packet: rtc.DataPacket) -> None:
        """只信当前传输身份与目标 SID；有界固定协议不向模型或对话历史转发。"""
        sender = packet.participant
        if (
            self._closed
            or self._ending
            or self._owner_identity is None
            or packet.topic != DELIVERY_TOPIC
            or packet.kind != rtc.DataPacketKind.KIND_RELIABLE
            or len(packet.data) > MAX_CONTROL_BYTES
            or sender is None
            or sender.identity != self._owner_identity
            or sender.sid != self._owner_sid
        ):
            return
        current = self._room.remote_participants.get(sender.identity)
        if current is None or current.sid != sender.sid:
            return
        try:
            payload = json.loads(packet.data)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"v", "type", "request_id", "target_agent_sid"}
                or type(payload["v"]) is not int
                or payload["v"] != 1
                or payload["type"] != "user_end"
                or payload["target_agent_sid"] != self._room.local_participant.sid
                or not isinstance(payload["request_id"], str)
                or str(UUID(payload["request_id"])) != payload["request_id"]
            ):
                return
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            return
        self._ending = True
        self._cancel_recovery()
        self._ack_task = asyncio.create_task(self._acknowledge_end(payload["request_id"]))

    async def _acknowledge_end(self, request_id: str) -> None:
        """确认是快速收尾信号而非成功前提，网络故障不得无限保留会话资源。"""
        try:
            if self._room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
                payload = json.dumps(
                    {
                        "v": 1,
                        "type": "user_end_ack",
                        "request_id": request_id,
                        "agent_sid": self._room.local_participant.sid,
                    },
                    separators=(",", ":"),
                )
                async with asyncio.timeout(ACK_TIMEOUT):
                    await self._room.local_participant.publish_data(
                        payload,
                        reliable=True,
                        topic=DELIVERY_TOPIC,
                        destination_identities=[self._owner_identity],
                    )
        except Exception:
            logger.warning("结束确认未能发送，继续回收会话")
        finally:
            if not self._closed:
                self._notify_end()

    def _notify_end(self) -> None:
        """所有主动结束入口共享单次通知，后续释放仍由原适配器统一负责。"""
        if self._closed or self._notified:
            return
        self._ending = True
        self._notified = True
        self._cancel_recovery()
        self._on_end()

    async def aclose(self) -> None:
        """重复关闭共用任务，外部等待者取消不能留下监听、计时器或确认请求。"""
        if self._close_task is None:
            self._closed = True
            self._cancel_recovery()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """取消自有等待后尝试注销全部回调，单点异常也不能跳过任务回收。"""
        tasks = [task for task in (self._ready_task, self._ack_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            if self._listening:
                self._listening = False
                with ExitStack() as cleanup:
                    cleanup.callback(self._room.off, "participant_connected", self._on_connected)
                    cleanup.callback(
                        self._room.off, "participant_disconnected", self._on_disconnected
                    )
                    cleanup.callback(self._room.off, "data_received", self._on_data)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
