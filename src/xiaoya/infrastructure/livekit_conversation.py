"""将 LiveKit 的会话生命周期适配为应用层业务端口。"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    InterruptionOptions,
    TurnHandlingOptions,
    inference,
    tts,
)
from livekit.agents.voice.room_io import RoomOptions, TextOutputOptions
from livekit.plugins import openai, silero

from xiaoya.application.prepare_delivery import PrepareDelivery
from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter
from xiaoya.infrastructure.expressive_agent import VOICE_INSTRUCTIONS, ExpressiveAgent
from xiaoya.infrastructure.mcp_tools import MCPTools
from xiaoya.infrastructure.settings import Settings
from xiaoya.infrastructure.speech_tokenizer import SpeechSentenceTokenizer
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
    ) -> None:
        """工具按 Job 注入；DeepSeek 复用 Chat Completions，语音模式避免等待深度思考。"""
        self._room = room
        self._tools = tools
        self._mcp = mcp
        self._subscriptions: list[tuple[Any, str, Any]] = []
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._expressive_enabled = settings.expressive_enabled
        self._expressive_agent: ExpressiveAgent | None = None
        if settings.stt_protocol == "websocket":
            self._stt = LocalStreamingSTT(
                model=settings.stt_model,
                language=settings.language,
                base_url=settings.stt_base_url,
                api_key=settings.stt_api_key or "not-required",
                vad_model=vad,
            )
        else:
            self._stt = openai.STT(
                model=settings.stt_model,
                language=settings.language,
                base_url=settings.stt_base_url,
                api_key=settings.stt_api_key or "not-required",
                use_realtime=False,
            )
        self._llm = openai.LLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "not-required",
            extra_body=settings.llm_extra_body,
        )
        self._tts = openai.TTS(
            model=settings.tts_model,
            voice=settings.tts_voice,
            base_url=settings.tts_base_url,
            api_key=settings.tts_api_key or "not-required",
            response_format=settings.tts_response_format,
        )
        self._tts_stream = tts.StreamAdapter(
            tts=self._tts, sentence_tokenizer=SpeechSentenceTokenizer()
        )
        self._voices = {"neutral": self._tts}
        if self._expressive_enabled:
            for preset, instructions in VOICE_INSTRUCTIONS.items():
                self._voices[preset] = openai.TTS(
                    model=settings.tts_model,
                    voice=settings.tts_voice,
                    base_url=settings.tts_base_url,
                    api_key=settings.tts_api_key or "not-required",
                    response_format=settings.tts_response_format,
                    instructions=instructions,
                )
        self._session = AgentSession(
            # 默认 Markdown 过滤会把 [style|gesture](正文) 当链接；只能在解析控制头后过滤正文。
            **({"tts_text_transforms": []} if self._expressive_enabled else {}),
            stt=self._stt,
            llm=self._llm,
            tts=self._tts_stream,
            vad=vad,
            turn_handling=TurnHandlingOptions(
                turn_detection=inference.TurnDetector(version="v1-mini"),
                endpointing={"min_delay": 0.90, "max_delay": 1.10},
                preemptive_generation={"enabled": True, "preemptive_tts": True},
                interruption=InterruptionOptions(mode="vad"),
            ),
        )

    async def start(self, profile: AssistantProfile) -> None:
        """先确认工具可用再启动语音；任一启动失败都回收资源并阻止开场白。"""
        try:
            tools = self._tools.tools() if self._tools is not None else []
            if self._mcp is not None:
                tools.extend(await self._mcp.tools())
            self._bind_session_events()
            if self._expressive_enabled:
                self._expressive_agent = ExpressiveAgent(
                    instructions=profile.instructions,
                    tools=tools,
                    room=self._room,
                    voices=self._voices,
                )
                await self._session.start(
                    room=self._room,
                    agent=self._expressive_agent,
                    room_options=RoomOptions(
                        text_output=TextOutputOptions(sync_transcription=False)
                    ),
                )
                # SDK 并发连接与 on_enter，start 返回后再兜底，必须早于业务开场白。
                await self._expressive_agent.enable_delivery()
            else:
                await self._session.start(
                    room=self._room, agent=Agent(instructions=profile.instructions, tools=tools)
                )
            if self._closed:
                raise RuntimeError("语音会话在启动完成前已关闭")
        except BaseException:
            try:
                await self.close()
            except Exception:
                logger.exception("启动失败后的语音资源回收异常")
            raise

    def _bind_session_events(self) -> None:
        """启动前监听严重错误和 SDK 主动结束，避免浏览器动画迁移后遗漏语音资源回收。"""
        for event, handler in (
            ("error", self._on_error),
            ("close", self._on_session_close),
        ):
            self._session.on(event, handler)
            self._subscriptions.append((self._session, event, handler))

    def _on_error(self, event: Any) -> None:
        """可恢复模型错误留给 SDK 重试，严重错误回收工具与模型，避免闲置连接泄漏。"""
        if not event.error.recoverable:
            self._schedule_close()

    def _on_session_close(self, event: Any) -> None:
        """SDK 主动结束与 Job 回收共享关闭任务，回调不会递归创建第二次资源释放。"""
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
        if self._expressive_enabled:
            segment = PrepareDelivery().greeting(text)
            await self._session.say(segment.intent.marker + segment.text)
        else:
            await self._session.say(text)

    async def close(self) -> None:
        """重复或并发关闭共享结果，防止 SDK 主动结束与 Job 回收重复释放资源。"""
        self._schedule_close()
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        """先停会话再回收 MCP 与模型，避免正在执行的工具访问已关闭连接。"""
        for emitter, event, handler in self._subscriptions:
            emitter.off(event, handler)
        self._subscriptions.clear()
        async with AsyncExitStack() as cleanup:
            if self._mcp is not None:
                cleanup.push_async_callback(self._mcp.close)
            for model in (self._stt, self._llm, *self._voices.values(), self._tts_stream):
                cleanup.push_async_callback(model.aclose)
            if self._expressive_agent is not None:
                cleanup.push_async_callback(self._expressive_agent.aclose)
            cleanup.push_async_callback(self._session.aclose)
