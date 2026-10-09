"""将 LiveKit 的会话生命周期适配为应用层业务端口。"""

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

from livekit import rtc
from livekit.agents import (
    AgentSession,
    InterruptionOptions,
    TurnHandlingOptions,
    inference,
)
from livekit.agents.voice.room_io import RoomOptions, TextOutputOptions
from livekit.plugins import openai, silero

from xiaoya.application.prepare_delivery import PrepareDelivery
from xiaoya.domain.assistant import AssistantEnvironment, AssistantProfile
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter
from xiaoya.infrastructure.conversation_connection import ConversationConnection
from xiaoya.infrastructure.delivery_output import DeliverySnapshotEndpoint, DeliveryState
from xiaoya.infrastructure.expressive_agent import VOICE_INSTRUCTIONS, ExpressiveAgent
from xiaoya.infrastructure.mcp_tools import MCPTools
from xiaoya.infrastructure.settings import Settings
from xiaoya.infrastructure.streaming_stt import LocalStreamingSTT

logger = logging.getLogger(__name__)


class LiveKitVoiceConversation:
    """模型和会话按 Job 隔离，仅复用进程级 VAD 的模型权重。"""

    def __init__(
        self,
        *,
        room: rtc.Room,
        settings: Settings,
        vad: silero.VAD,
        tools: AssistantToolAdapter | None = None,
        mcp: MCPTools | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> None:
        """工具、快照端点和终止通知按 Job 隔离，运行态主动结束只在完整清理后通知宿主。"""
        self._room = room
        self._tools = tools
        self._mcp = mcp
        self._on_terminal = on_terminal
        self._subscriptions: list[tuple[Any, str, Any]] = []
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._started = False
        self._terminal_requested = False
        self._terminal_notified = False
        self._settings = settings
        self._vad = vad
        self._starting = False
        self._models: list[Any] = []
        self._voices: dict[str, openai.TTS] = {}
        self._session: AgentSession | None = None
        self._connection = ConversationConnection(room, self._on_connection_end)
        self._expressive_agent: ExpressiveAgent | None = None
        self._snapshot_endpoint: DeliverySnapshotEndpoint | None = None

    def _initialize_models(self) -> None:
        """中间客户端只用局部变量，成功即登记唯一清理清单，下一步失败也能精确回收。"""
        settings = self._settings
        stt = self._own_model(
            LocalStreamingSTT(
                model=settings.stt_model,
                language=settings.language,
                base_url=settings.stt_base_url,
                api_key=settings.stt_api_key,
                vad_model=self._vad,
            )
        )
        llm = self._own_model(
            openai.LLM(
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                extra_body=settings.llm_extra_body,
            )
        )
        tts = self._own_model(
            openai.TTS(
                model=settings.tts_model,
                voice=settings.tts_voice,
                base_url=settings.tts_base_url,
                api_key=settings.tts_api_key,
                response_format=settings.tts_response_format,
            )
        )
        self._voices = {"neutral": tts}
        for preset, instructions in VOICE_INSTRUCTIONS.items():
            self._voices[preset] = self._own_model(
                openai.TTS(
                    model=settings.tts_model,
                    voice=settings.tts_voice,
                    base_url=settings.tts_base_url,
                    api_key=settings.tts_api_key,
                    response_format=settings.tts_response_format,
                    instructions=instructions,
                )
            )
        self._session = AgentSession(
            # 内部头只在公开节点解码，SDK 不能提前把带括号的正文当 Markdown 链接。
            tts_text_transforms=[],
            stt=stt,
            llm=llm,
            tts=tts,
            vad=self._vad,
            turn_handling=TurnHandlingOptions(
                turn_detection=inference.TurnDetector(version="v1-mini"),
                endpointing={"min_delay": 0.90, "max_delay": 1.10},
                preemptive_generation={"enabled": True, "preemptive_tts": True},
                interruption=InterruptionOptions(mode="vad"),
            ),
        )

    def _own_model(self, model: Any) -> Any:
        """登记构造成功的资源，不依赖全部模型均成功后才建立清理清单。"""
        self._models.append(model)
        return model

    async def start(self, profile: AssistantProfile) -> None:
        """启动与失败清理共用所有权；绑定后确认真实环境，console 仅保留通道差异。"""
        if self._starting or self._closed:
            raise RuntimeError("语音会话只能启动一次")
        self._starting = True
        try:
            self._initialize_models()
            tools = self._tools.tools() if self._tools is not None else []
            if self._mcp is not None:
                tools.extend(await self._mcp.tools())
            if self._closed:
                raise RuntimeError("语音会话在启动完成前已关闭")
            environment = AssistantEnvironment(
                tool_names=tuple(tool.info.name for tool in tools),
                runtime_platform=(
                    "Windows"
                    if sys.platform == "win32"
                    else "Linux"
                    if sys.platform == "linux"
                    else "其他"
                ),
            )
            self._bind_session_events()
            self._connection.listen()
            self._expressive_agent = ExpressiveAgent(
                instructions=profile.instructions,
                tools=tools,
                room=self._room,
                voices=self._voices,
                environment=environment,
            )
            agent = self._expressive_agent
            await self._session.start(
                room=self._room,
                agent=agent,
                room_options=RoomOptions(
                    close_on_disconnect=False,
                    text_output=TextOutputOptions(sync_transcription=False),
                ),
            )
            if self._closed:
                raise RuntimeError("语音会话在启动完成前已关闭")
            try:
                room_io = self._session.room_io
            except RuntimeError:
                # console 由 AgentsConsole 接管 IO，公开属性在此模式明确报告不存在。
                await self._connection.aclose()
                channel = "console"
            else:
                self._snapshot_endpoint = DeliverySnapshotEndpoint(
                    self._room, ready=self._snapshot_ready, state=self._delivery_snapshot
                )
                await agent.enable_delivery(self._snapshot_endpoint)
                await self._connection.bind(room_io)
                channel = _room_channel(self._room.metadata)
            if self._closed:
                raise RuntimeError("语音会话在启动完成前已关闭")
            agent.set_environment(replace(environment, channel=channel))
            self._started = True
        except BaseException:
            try:
                await self.close()
            except Exception:
                logger.exception("启动失败后的语音资源回收异常")
            raise

    def _snapshot_ready(self, identity: str) -> bool:
        """SDK 已连接不等于会话已就绪，启动与关闭门和当前用户认证必须同时通过。"""
        return self._started and not self._closed and self._connection.is_owner(identity)

    def _delivery_snapshot(self) -> DeliveryState | None:
        """只读取已实际输出的状态；尚未输出或已关闭时共享端点返回稳定中性快照。"""
        return (
            self._expressive_agent.delivery_snapshot if self._expressive_agent is not None else None
        )

    def _bind_session_events(self) -> None:
        """启动前监听严重错误和 SDK 主动结束，避免浏览器动画迁移后遗漏语音资源回收。"""
        for event, handler in (
            ("error", self._on_error),
            ("close", self._on_session_close),
        ):
            self._session.on(event, handler)
            self._subscriptions.append((self._session, event, handler))

    def _on_error(self, event: Any) -> None:
        """可恢复错误留给 SDK；运行态严重错误请求终止，普通 Job 回收后的迟到事件不反向通知。"""
        if not event.error.recoverable:
            if self._started and not self._closed:
                self._terminal_requested = True
            self._schedule_close()

    def _on_session_close(self, event: Any) -> None:
        """SDK 的锁内事件仅调度清理；只有运行态首次主动关闭才请求清理后的 Job 终止。"""
        if self._started and not self._closed:
            self._terminal_requested = True
        self._schedule_close()

    def _on_connection_end(self) -> None:
        """明确挂断或恢复超时复用既有清理顺序，不重放会话、开场白或工具。"""
        if self._started and not self._closed:
            self._terminal_requested = True
            self._schedule_close()

    def _schedule_close(self) -> None:
        """从同步 SDK 回调启动受监督的回收，异常不会成为无人读取的任务错误。"""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_resources())
            self._close_task.add_done_callback(self._observe_close)

    @staticmethod
    def _observe_close(task: asyncio.Task[None]) -> None:
        """Job 回收仍会等待同一任务，回调仅确保主动关闭异常及时进入日志。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("语音会话回收失败", exc_info=(type(error), error, error.__traceback__))

    async def say(self, text: str) -> None:
        """固定开场白直接交给 TTS，节省一次生成并保持业务文案确定。"""
        if not self._started or self._closed or self._session is None:
            raise RuntimeError("语音会话尚未启动或已经关闭")
        segment = PrepareDelivery().greeting(text)
        await self._session.say(segment.intent.marker + segment.text)

    async def close(self) -> None:
        """重复或并发关闭共享结果，防止 SDK 主动结束与 Job 回收重复释放资源。"""
        self._schedule_close()
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        """停止 SDK 后注销唯一快照，再释放表现与模型；全部清理尝试结束后才通知 Job。"""
        try:
            async with AsyncExitStack() as cleanup:
                if self._mcp is not None:
                    cleanup.push_async_callback(self._mcp.close)
                for model in self._models:
                    cleanup.push_async_callback(model.aclose)
                if self._expressive_agent is not None:
                    cleanup.push_async_callback(self._expressive_agent.aclose)
                if self._snapshot_endpoint is not None:
                    cleanup.push_async_callback(self._snapshot_endpoint.aclose)
                if self._session is not None:
                    cleanup.push_async_callback(self._session.aclose)
                for emitter, event, handler in self._subscriptions:
                    cleanup.callback(emitter.off, event, handler)
                self._subscriptions.clear()
                cleanup.push_async_callback(self._connection.aclose)
        finally:
            if self._terminal_requested and not self._terminal_notified:
                self._terminal_notified = True
                if self._on_terminal is not None:
                    self._on_terminal()


def _room_channel(metadata: str) -> str:
    """只认可令牌端签名的固定来源枚举，不把任意房名或原始 metadata 当模型指令。"""
    if not isinstance(metadata, str):
        return "room"
    try:
        value = json.loads(metadata)
    except (ValueError, TypeError, RecursionError):
        return "room"
    if (
        isinstance(value, dict)
        and value == {"xiaoya": {"v": 1, "client": "web"}}
        and type(value["xiaoya"]["v"]) is int
    ):
        return "web"
    return "room"
