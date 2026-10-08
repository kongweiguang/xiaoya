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

from xiaoya.domain.delivery import DeliveryIntent

DELIVERY_TOPIC = "xiaoya.delivery"
DELIVERY_SNAPSHOT_RPC = "xiaoya.getDeliverySnapshot"
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
    v: int = 1

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
    ) -> None:
        """注册失败交由会话启动传播；稳定中性实例让关闭表现的会话仍能证明真实就绪。"""
        self._room = room
        self._participant = room.local_participant
        self._ready = ready
        self._state = state
        self._fallback = DeliveryState(instance=instance or uuid4().hex)
        self._registered = False
        self._closed = False
        try:
            self._participant.register_rpc_method(DELIVERY_SNAPSHOT_RPC, self._snapshot)
        except Exception:
            # SDK 可能先保存回调再注册 FFI；只清理本入口，已捕获的回调也必须永远关门。
            self._closed = True
            with contextlib.suppress(Exception):
                self._participant.unregister_rpc_method(DELIVERY_SNAPSHOT_RPC)
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

    async def aclose(self) -> None:
        """同步撤销授权后仅注销一次，无网络等待，也不让单点失败阻止其他资源回收。"""
        if self._closed:
            return
        self._closed = True
        registered, self._registered = self._registered, False
        if registered:
            try:
                self._participant.unregister_rpc_method(DELIVERY_SNAPSHOT_RPC)
            except Exception as error:
                _LOGGER.warning("注销会话快照失败（%s）", type(error).__name__)


@dataclass(frozen=True, slots=True)
class _Binding:
    """每个入队操作捕获自己的身份，后续绑定不能修改旧字幕的头信息。"""

    reply_id: str
    segment_id: str
    intent: DeliveryIntent


@dataclass(frozen=True, slots=True)
class _Operation:
    """同步 flush 与异步网络共用单队列，保持开流、正文和关闭的调用顺序。"""

    kind: Literal["text", "flush", "cancel"]
    binding: _Binding | None = None
    text: str = ""
    reply_id: str = ""
    done: asyncio.Future[None] | None = None


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
    ) -> None:
        """输出只借用会话快照；所有权不随字幕故障变化，超时仅约束装饰通道。"""
        super().__init__(label="xiaoya.delivery", next_in_chain=None)
        if io_timeout <= 0:
            raise ValueError("字幕网络超时必须大于零")
        if not snapshot_endpoint.registered:
            raise ValueError("表现输出需要已登记的会话快照")
        self._room = room
        self._participant = room.local_participant
        self._io_timeout = io_timeout
        self._state = DeliveryState(instance=snapshot_endpoint.instance)
        self._bound: _Binding | None = None
        self._segment: _Segment | None = None
        self._cancelled_replies: set[str] = set()
        self._queue: asyncio.Queue[_Operation] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._accepting = True
        self._snapshot_endpoint = snapshot_endpoint

    @property
    def snapshot(self) -> DeliveryState:
        """只暴露不可变状态，供装配和测试读取而不允许绕过串行更新。"""
        return self._state

    async def bind_segment(self, reply_id: str, intent: DeliveryIntent) -> None:
        """调用者须先等待同步器 barrier；绑定本身不证明开始播放，也不发送消息。"""
        if not self._accepting:
            return
        if not reply_id:
            raise ValueError("表达句段必须绑定已获播放许可的回复身份")
        if self._bound is not None:
            self.flush()
        self._bound = _Binding(
            reply_id=reply_id,
            segment_id=f"SG_{uuid4().hex}",
            intent=DeliveryIntent.normalize(intent.style, intent.gesture),
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
        """关闭操作捕获旧绑定，不能在异步执行时误关已经开始的新句段。"""
        if not self._accepting or self._bound is None:
            return
        binding, self._bound = self._bound, None
        self._enqueue(_Operation("flush", binding=binding))

    async def cancel_reply(self, reply_id: str) -> None:
        """先隔离尚未输出的取消回复，再等待其精确关闭，旧回调不影响新回复。"""
        if not reply_id or not self._accepting:
            return
        self._cancelled_replies.add(reply_id)
        done = asyncio.get_running_loop().create_future()
        self._enqueue(_Operation("cancel", reply_id=reply_id, done=done))
        await asyncio.shield(done)

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
                if operation.kind == "text" and operation.binding is not None:
                    await self._write_text(operation.binding, operation.text)
                elif operation.kind == "flush":
                    if self._segment and self._segment.binding == operation.binding:
                        await self._finish_segment()
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
        """多次网络 await 后均重新检查取消，防止排队的旧正文恢复已取消的表情。"""
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
            await self._open_segment(segment)
        if segment is None or segment.failed or segment.writer is None:
            return
        if self._bound != binding:
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
        elif self._bound != binding:
            await self._close_expression(binding)

    async def _open_segment(self, segment: _Segment) -> None:
        """共享入口仍有效才创建 active；失去快照能力时继续纯字幕，不伪造可恢复的表达许可。"""
        binding = segment.binding
        attributes = {
            "lk.segment_id": binding.segment_id,
            "lk.transcription_final": "false",
        }
        if binding.reply_id and self._snapshot_endpoint.registered:
            live = self._bound == binding
            intent = binding.intent if live else DeliveryIntent()
            self._state = DeliveryState(
                instance=self._state.instance,
                revision=self._state.revision + 1,
                reply_id=binding.reply_id,
                segment_id=binding.segment_id,
                state="active" if live else "closed",
                style=intent.style,
                gesture=intent.gesture,
            )
            success = await self._publish_state()
            if not success or binding.reply_id in self._cancelled_replies or self._bound != binding:
                await self._close_expression(binding)
            attributes[DELIVERY_TOPIC] = self._state.to_json()
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

    async def _finish_segment(self) -> None:
        """关闭先撤销权威许可，再尽力结束字幕流；旧 stream 不持有下一段的状态。"""
        segment = self._segment
        if segment is None:
            return
        await self._close_expression(segment.binding)
        await self._close_writer(segment)
        if self._segment is segment:
            self._segment = None

    async def _close_expression(self, binding: _Binding) -> None:
        """同时比对回复与句段，迟到的取消或异常不能关闭更新的权威版本。"""
        if (
            self._state.state != "active"
            or self._state.reply_id != binding.reply_id
            or self._state.segment_id != binding.segment_id
        ):
            return
        self._state = DeliveryState(
            instance=self._state.instance,
            revision=self._state.revision + 1,
            reply_id=binding.reply_id,
            segment_id=binding.segment_id,
        )
        await self._publish_state()

    async def _close_writer(self, segment: _Segment) -> None:
        """清空拥有权后再关闭，超时或重复清理都不会再次消费同一个 SDK writer。"""
        writer, segment.writer = segment.writer, None
        if writer is not None:
            await self._network(
                writer.aclose(attributes={"lk.transcription_final": "true"}), "字幕收尾"
            )

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
        """表现只回收自身队列和字幕，共享快照始终由会话独立回收。"""
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
        await self._finish_segment()

    @staticmethod
    def _warn(operation: str, error: Exception) -> None:
        """日志只描述失败类别，不记录用户正文、控制消息或服务端可能返回的敏感内容。"""
        _LOGGER.warning("%s失败，表达保持中性（%s）", operation, type(error).__name__)
