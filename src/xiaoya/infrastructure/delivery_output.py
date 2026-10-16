"""用公开字幕与房间消息接口传递已获播放许可的表达，不接触预生成结果。"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Literal, TypeVar
from uuid import uuid4

from livekit import rtc
from livekit.agents.voice import io

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationCue

DELIVERY_TOPIC = "xiaoya.delivery"
DELIVERY_SNAPSHOT_RPC = "xiaoya.getDeliverySnapshot"
PRESENTATION_ACK_RPC = "xiaoya.ackPresentation"
TRANSCRIPTION_TOPIC = "lk.transcription"
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryState:
    """头信息、实时通知与补取快照共用同一版本，禁止各通道另行推断当前表情。"""

    instance: str
    revision: int = 0
    reply_id: str = ""
    segment_id: str = ""
    state: Literal["active", "closed"] = "closed"
    style: str = "neutral"
    gesture: str = "none"
    phase: Literal["idle", "listening", "thinking", "speaking"] = "idle"
    turn_id: str = ""
    gesture_id: str = ""
    gesture_source: Literal["none", "dialogue", "command"] = "none"
    command_feedback: Literal["none", "retry", "clarify"] = "none"
    energy: float = 0.5
    v: int = 3

    def to_json(self) -> str:
        """小型控制消息不带正文，重连快照无需读取或泄露聊天记录。"""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class DeliverySnapshotEndpoint:
    """就绪校验独立于可选表现输出，同一房间会话只拥有一个公开快照入口。"""

    def __init__(
        self,
        room: rtc.Room,
        *,
        ready: Callable[[str], bool],
        state: Callable[[], DeliveryState | None],
        instance: str | None = None,
        acknowledge: Callable[[str, str, str, bool | None], bool] | None = None,
    ) -> None:
        """注册失败交由会话启动传播；稳定中性实例让关闭表现的会话仍能证明真实就绪。"""
        self._room = room
        self._participant = room.local_participant
        self._ready = ready
        self._state = state
        self._acknowledge = acknowledge
        self._fallback = DeliveryState(instance=instance or uuid4().hex)
        self._registered = False
        self._closed = False
        try:
            self._participant.register_rpc_method(DELIVERY_SNAPSHOT_RPC, self._snapshot)
            self._participant.register_rpc_method(PRESENTATION_ACK_RPC, self._ack)
        except Exception:
            # SDK 可能先保存回调再注册 FFI；只清理本入口，已捕获的回调也必须永远关门。
            self._closed = True
            with contextlib.suppress(Exception):
                self._participant.unregister_rpc_method(DELIVERY_SNAPSHOT_RPC)
            with contextlib.suppress(Exception):
                self._participant.unregister_rpc_method(PRESENTATION_ACK_RPC)
            raise
        self._registered = True

    @property
    def instance(self) -> str:
        """实例由入口唯一生成，装饰输出不能额外建立第二个身份空间。"""
        return self._fallback.instance

    @property
    def registered(self) -> bool:
        """只暴露本地拥有权，注销失败也不能让迟到调用继续得到授权。"""
        return self._registered and not self._closed

    async def _snapshot(self, data: rtc.RpcInvocationData) -> str:
        """先验证传输身份与业务就绪再读状态；表现故障只能降为中性，不能放宽授权。"""
        identity = data.caller_identity
        if (
            not self.registered
            or not identity
            or identity not in self._room.remote_participants
            or not self._ready(identity)
        ):
            raise rtc.RpcError(2001, "当前参与者尚未就绪或无权读取会话状态")
        try:
            state = self._state()
            if state is None:
                state = self._fallback
            elif state.instance != self.instance:
                raise ValueError("表现状态实例与会话不一致")
            return state.to_json()
        except Exception as error:
            _LOGGER.warning("读取表现状态失败，返回中性快照（%s）", type(error).__name__)
            return self._fallback.to_json()

    async def _ack(self, data: rtc.RpcInvocationData) -> str:
        """同一v3只附可选真实stroke布尔；缺字段仍是未知，非法值和非所有者不能解除预算。"""
        identity = data.caller_identity
        if (
            not self.registered
            or identity not in self._room.remote_participants
            or not self._ready(identity)
        ):
            raise rtc.RpcError(2001, "当前参与者无权确认人物动作")
        try:
            if len(data.payload) > 1024:
                raise ValueError("回执过大")
            body = json.loads(data.payload)
            if (
                not isinstance(body, dict)
                or set(body)
                not in (
                    {"instance", "turn_id", "gesture_id", "status"},
                    {"instance", "turn_id", "gesture_id", "status", "stroke_started"},
                )
                or any(
                    not isinstance(body[key], str) or not body[key]
                    for key in ("instance", "turn_id", "gesture_id", "status")
                )
                or ("stroke_started" in body and type(body["stroke_started"]) is not bool)
                or body["instance"] != self.instance
                or body["status"] not in {"started", "completed", "canceled", "unavailable"}
                or (
                    body.get("stroke_started") is False
                    and body["status"] in {"started", "completed"}
                )
            ):
                raise ValueError("回执身份或状态非法")
            accepted = self._acknowledge is not None and self._acknowledge(
                body["turn_id"], body["gesture_id"], body["status"], body.get("stroke_started")
            )
            return json.dumps({"accepted": accepted}, separators=(",", ":"))
        except (ValueError, TypeError, KeyError) as error:
            raise rtc.RpcError(2002, "人物回执无效") from error

    async def aclose(self) -> None:
        """同步撤销授权后仅注销一次，无网络等待，也不让单点失败阻止其他资源回收。"""
        if self._closed:
            return
        self._closed = True
        registered, self._registered = self._registered, False
        if registered:
            for name in (DELIVERY_SNAPSHOT_RPC, PRESENTATION_ACK_RPC):
                try:
                    self._participant.unregister_rpc_method(name)
                except Exception as error:
                    _LOGGER.warning("注销会话表现入口失败（%s）", type(error).__name__)


@dataclass(frozen=True, slots=True)
class _Binding:
    """每个入队操作捕获自己的身份，后续绑定不能修改旧字幕的头信息。"""

    reply_id: str
    segment_id: str
    intent: DeliveryIntent
    turn_id: str = ""
    text: str = ""


@dataclass(frozen=True, slots=True)
class _Operation:
    """同步 flush 与异步网络共用单队列，保持开流、正文和关闭的调用顺序。"""

    kind: Literal["text", "flush", "cancel", "present"]
    binding: _Binding | None = None
    text: str = ""
    reply_id: str = ""
    done: asyncio.Future[None] | None = None
    state: DeliveryState | None = None


@dataclass(slots=True)
class _Segment:
    """只有工作队列可以改写传输状态，绑定内容始终保持不可变。"""

    binding: _Binding
    writer: rtc.TextStreamWriter | None = None
    failed: bool = False
    opened: bool = False
    prefix: str = ""


class DeliveryTextOutput(io.TextOutput):
    """同步器之后的尽力输出层：字幕失败不终止音频，表达异常统一关门。"""

    def __init__(
        self,
        room: rtc.Room,
        *,
        io_timeout: float = 1.0,
        snapshot_endpoint: DeliverySnapshotEndpoint,
        presentation: PresentationController | None = None,
    ) -> None:
        """正文输入与实际播放分开持有身份，字幕flush不能让仍在播放的短句提前闭嘴。"""
        super().__init__(label="xiaoya.delivery", next_in_chain=None)
        if io_timeout <= 0:
            raise ValueError("字幕网络超时必须大于零")
        if not snapshot_endpoint.registered:
            raise ValueError("表现输出需要已登记的会话快照")
        self._participant = room.local_participant
        self._io_timeout = io_timeout
        self._state = DeliveryState(instance=snapshot_endpoint.instance)
        self._bound: _Binding | None = None
        self._playback_binding: _Binding | None = None
        self._segment: _Segment | None = None
        self._cancelled_replies: set[str] = set()
        self._queue: asyncio.Queue[_Operation] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._accepting = True
        self._snapshot_endpoint = snapshot_endpoint
        self._presentation = presentation or PresentationController()
        self._presentation.attach(self.present)

    @property
    def snapshot(self) -> DeliveryState:
        """只暴露不可变状态，供装配和测试读取而不允许绕过串行更新。"""
        return self._state

    async def bind_segment(self, reply_id: str, intent: DeliveryIntent, text: str = "") -> None:
        """下一段撤销旧播放身份；正文完成后仍保留独立播放绑定，等待SDK真实结束。"""
        if not self._accepting:
            return
        if not reply_id:
            raise ValueError("表达句段必须绑定已获播放许可的回复身份")
        if self._bound is not None:
            self.flush()
        if self._playback_binding is not None and self._revoke_expression(self._playback_binding):
            self._enqueue(_Operation("present", state=self._state))
        self._bound = _Binding(
            reply_id=reply_id,
            segment_id=f"SG_{uuid4().hex}",
            intent=DeliveryIntent.normalize(intent.style, intent.gesture),
            turn_id=self._presentation.turn_id,
            text=text,
        )
        self._playback_binding = self._bound

    def present(self, cue: PresentationCue) -> None:
        """思考预判保留同轮未开流候选；实际空闲、倾听或换轮同步撤销旧播放身份。"""
        if not self._accepting:
            return
        if (
            cue.gesture_source != "command"
            and cue.phase == "speaking"
            and (
                self._playback_binding is None
                or (cue.reply_id, cue.segment_id)
                != (self._playback_binding.reply_id, self._playback_binding.segment_id)
                or cue.reply_id in self._cancelled_replies
            )
        ):
            return
        binding = self._playback_binding
        if (
            cue.phase != "speaking"
            and binding is not None
            and (
                cue.phase in {"idle", "listening"}
                or cue.turn_id != binding.turn_id
                or self._state.phase == "speaking"
            )
        ):
            self._playback_binding = None
        self._set_cue(cue)
        self._enqueue(_Operation("present", state=self._state))

    def _set_cue(self, cue: PresentationCue) -> None:
        """协议版本统一映射领域值对象，字幕头与消息快照复用相同稳定片段身份。"""
        self._state = DeliveryState(
            instance=self._state.instance,
            revision=self._state.revision + 1,
            reply_id=cue.reply_id,
            segment_id=cue.segment_id,
            state="closed" if cue.phase == "idle" and cue.gesture_source != "command" else "active",
            style=cue.intent.style,
            gesture=cue.intent.gesture,
            phase=cue.phase,
            turn_id=cue.turn_id,
            gesture_id=cue.gesture_id,
            gesture_source=cue.gesture_source,
            command_feedback=cue.command_feedback,
            energy=cue.energy,
        )

    async def capture_text(self, text: str) -> None:
        """只排队同步器已放行的正文，不让消息网络阻塞音频或传播输出异常。"""
        if not self._accepting or not text:
            return
        if self._bound is None:
            # 无绑定仍可显示普通字幕，但不伪造当前 reply 或授予表达许可。
            self._bound = _Binding("", f"SG_{uuid4().hex}", DeliveryIntent())
        self._enqueue(_Operation("text", binding=self._bound, text=text))

    def flush(self) -> None:
        """flush只完成字幕输入；中文单字的字幕可以早于真实音频尾部，不能据此撤销播放。"""
        if not self._accepting or self._bound is None:
            return
        binding, self._bound = self._bound, None
        self._enqueue(_Operation("flush", binding=binding))

    async def cancel_reply(self, reply_id: str) -> None:
        """取消立即撤销精确播放身份，网络收尾排队也不能让旧口型或迟到Jev继续生效。"""
        if not reply_id or not self._accepting:
            return
        self._cancelled_replies.add(reply_id)
        binding = self._playback_binding
        if (
            binding is not None
            and binding.reply_id == reply_id
            and self._revoke_expression(binding)
        ):
            self._enqueue(_Operation("present", state=self._state))
        done = asyncio.get_running_loop().create_future()
        self._enqueue(_Operation("cancel", reply_id=reply_id, done=done))
        await asyncio.shield(done)

    async def finish_reply(self, reply_id: str) -> None:
        """公开SpeechHandle正常done封存完整对话，片段切换或取消不提前提交最近一轮事实。"""
        binding = self._playback_binding
        if not self._accepting or reply_id in self._cancelled_replies:
            return
        if binding is not None and binding.reply_id == reply_id:
            if self._bound == binding:
                self._bound = None
            await self._close_expression(binding)
        self._presentation.complete_reply(reply_id)

    async def drain(self) -> None:
        """仅在验证或显式关闭边界等待队列，正常音频路径不依赖消息发送耗时。"""
        await self._queue.join()

    async def aclose(self) -> None:
        """关闭幂等且屏蔽调用者取消，确保后台任务和公开 RPC 都获得回收机会。"""
        if self._close_task is None:
            self._accepting = False
            self._close_task = asyncio.create_task(self._shutdown(), name="xiaoya-delivery-close")
        await asyncio.shield(self._close_task)

    def _enqueue(self, operation: _Operation) -> None:
        """懒启动唯一工作任务，让同步 flush 与正文共享同一网络发送次序。"""
        self._queue.put_nowait(operation)
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="xiaoya-delivery-output")

    async def _run(self) -> None:
        """单项故障降级后继续后续句段，不把装饰性通道的错误抛入 SDK 音频任务。"""
        while True:
            operation = await self._queue.get()
            try:
                if operation.kind == "present":
                    if operation.state == self._state:
                        await self._publish_state()
                elif operation.kind == "text" and operation.binding is not None:
                    await self._write_text(operation.binding, operation.text)
                elif operation.kind == "flush":
                    if self._segment and self._segment.binding == operation.binding:
                        await self._finish_segment(revoke=False)
                elif self._segment and self._segment.binding.reply_id == operation.reply_id:
                    await self._finish_segment()
            except Exception as error:
                self._warn("字幕输出", error)
                await self._finish_segment()
            finally:
                if operation.done is not None and not operation.done.done():
                    operation.done.set_result(None)
                self._queue.task_done()

    async def _write_text(self, binding: _Binding, text: str) -> None:
        """网络await后校验实际播放绑定，正常文字flush不算取消，换段仍撤销旧表达。"""
        if binding.reply_id in self._cancelled_replies:
            return
        if self._segment is None or self._segment.binding != binding:
            await self._finish_segment()
            if binding.reply_id in self._cancelled_replies:
                return
            self._segment = _Segment(binding)
        segment = self._segment
        if not segment.opened:
            segment.prefix += text
            if not segment.prefix.strip():
                return
            text, segment.prefix = segment.prefix, ""
            segment.opened = True
            await self._open_segment(segment, text)
        if segment.failed or segment.writer is None:
            return
        if self._playback_binding != binding:
            await self._close_expression(binding)
        if binding.reply_id in self._cancelled_replies:
            await self._finish_segment()
            return
        success, _ = await self._network(segment.writer.write(text), "字幕正文")
        if not success:
            segment.failed = True
            await self._close_expression(binding)
            await self._close_writer(segment)
        elif binding.reply_id in self._cancelled_replies:
            await self._finish_segment()
        elif self._playback_binding != binding:
            await self._close_expression(binding)

    def _can_open_bound_segment(self, binding: _Binding) -> bool:
        """只给仍拥有播放许可的绑定开新流；不检查正文_bound，避免正常flush撤销短句。"""
        return (
            self._accepting
            and self._snapshot_endpoint.registered
            and self._playback_binding == binding
            and binding.turn_id == self._presentation.turn_id
            and binding.reply_id not in self._cancelled_replies
        )

    async def _open_segment(self, segment: _Segment, text: str) -> None:
        """尚未开流的绑定须在发布前后仍获许可；已开writer的尾词与无绑定字幕另循原路径。"""
        binding = segment.binding
        if binding.reply_id and not self._can_open_bound_segment(binding):
            return
        attributes = {
            "lk.segment_id": binding.segment_id,
            "lk.transcription_final": "false",
        }
        if binding.reply_id:
            self._set_cue(
                self._presentation.speak(
                    binding.reply_id, binding.segment_id, binding.intent, binding.text or text
                )
            )
            speaking_header = self._state.to_json()
            success = await self._publish_state()
            if (
                not success
                or not self._can_open_bound_segment(binding)
                or self._state.phase != "speaking"
                or (self._state.reply_id, self._state.segment_id)
                != (binding.reply_id, binding.segment_id)
            ):
                await self._close_expression(binding)
                return
            attributes[DELIVERY_TOPIC] = speaking_header
        if binding.reply_id in self._cancelled_replies:
            return
        success, writer = await self._network(
            self._participant.stream_text(topic=TRANSCRIPTION_TOPIC, attributes=attributes),
            "字幕开流",
        )
        if success:
            segment.writer = writer
        else:
            segment.failed = True
            await self._close_expression(binding)

    async def _finish_segment(self, *, revoke: bool = True) -> None:
        """普通flush只关闭字幕流；换段、取消、故障与关机仍同步撤销精确播放许可。"""
        segment = self._segment
        if segment is None:
            return
        if revoke:
            await self._close_expression(segment.binding)
        await self._close_writer(segment)
        if self._segment is segment:
            self._segment = None

    def _revoke_expression(self, binding: _Binding) -> bool:
        """内存许可先于网络撤销，回复和句段双重匹配防止旧结束事件关闭新说话。"""
        if self._playback_binding == binding:
            self._playback_binding = None
        if (
            self._state.state != "active"
            or self._state.reply_id != binding.reply_id
            or self._state.segment_id != binding.segment_id
        ):
            return False
        cue = self._presentation.finish(binding.reply_id, binding.segment_id)
        if cue is None:
            return False
        self._set_cue(cue)
        return True

    async def _close_expression(self, binding: _Binding) -> None:
        """权威撤销同步生效后尽力通知浏览器，控制网络失败不能恢复旧播放身份。"""
        if self._revoke_expression(binding):
            await self._publish_state()

    async def _close_writer(self, segment: _Segment) -> None:
        """关闭await被取消时保留writer交给关机重收；正常完成或失败才释放拥有权。"""
        writer = segment.writer
        if writer is not None:
            success, _ = await self._network(
                writer.aclose(attributes={"lk.transcription_final": "true"}), "字幕收尾"
            )
            if segment.writer is writer:
                segment.writer = None
            if not success:
                await self._close_expression(segment.binding)

    async def _publish_state(self) -> bool:
        """高频状态走可靠 data topic；当前内存快照始终可供重连后的 RPC 补取。"""
        success, _ = await self._network(
            self._participant.publish_data(
                self._state.to_json(), reliable=True, topic=DELIVERY_TOPIC
            ),
            "表达状态",
        )
        return success

    async def _network(self, operation: Awaitable[_T], name: str) -> tuple[bool, _T | None]:
        """限制装饰通道的单次等待；保留任务取消语义，但吞掉传输失败与超时。"""
        try:
            async with asyncio.timeout(self._io_timeout):
                return True, await operation
        except Exception as error:
            self._warn(name, error)
            return False, None

    async def _shutdown(self) -> None:
        """正文已经flush也必须关闭剩余播放身份；共享快照仍由会话独立回收。"""
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        while not self._queue.empty():
            operation = self._queue.get_nowait()
            if operation.done is not None and not operation.done.done():
                operation.done.set_result(None)
            self._queue.task_done()
        self._bound = None
        if self._playback_binding is not None:
            await self._close_expression(self._playback_binding)
        await self._finish_segment()

    @staticmethod
    def _warn(operation: str, error: Exception) -> None:
        """日志只描述失败类别，不记录用户正文、控制消息或服务端可能返回的敏感内容。"""
        _LOGGER.warning("%s失败，表达保持中性（%s）", operation, type(error).__name__)
