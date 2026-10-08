"""公开语音节点连接受控意图、私有 TTS 和实际播放后的字幕表现。"""

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace

from livekit import rtc
from livekit.agents import Agent, FlushSentinel, ModelSettings, llm, tts
from livekit.agents.voice.transcription import TranscriptSynchronizer, text_transforms

from xiaoya.application.assistant_tools import Clock
from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.domain.delivery import DELIVERY_INSTRUCTIONS, DeliveryIntent, SpeechSegment
from xiaoya.infrastructure.assistant_tools import SystemClock
from xiaoya.infrastructure.delivery_output import (
    DeliverySnapshotEndpoint,
    DeliveryState,
    DeliveryTextOutput,
)
from xiaoya.infrastructure.delivery_stream import DeliveryStreamParser

logger = logging.getLogger(__name__)
VOICE_INSTRUCTIONS = {
    "happy": "开心自然地说话。",
    "gentle": "轻柔温和地说话。",
}


class ExpressiveAgent(Agent):
    """文本分支携带不可变句段意图，TTS 预生成不能提前改变人物状态。"""

    def __init__(
        self,
        *,
        instructions: str,
        tools: list,
        room: rtc.Room,
        voices: dict[str, tts.TTS],
        environment: AssistantEnvironment | None = None,
        clock: Clock | None = None,
    ) -> None:
        """唯一语音链按实际渠道确认人物能力，输出仅由会话所有者在启动后装配。"""
        super().__init__(
            instructions=instructions,
            tools=tools,
            use_tts_aligned_transcript=False,
        )
        self._environment = environment or AssistantEnvironment()
        self._clock = clock or SystemClock()
        self._room = room
        self._voices = voices
        self._delivery_closed = False
        self._delivery: DeliveryTextOutput | None = None
        self._synchronizer: TranscriptSynchronizer | None = None
        self._speech_ids: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def environment(self) -> AssistantEnvironment:
        """不可变事实由装配入口更新，不从用户消息或旧历史推断运行能力。"""
        return self._environment

    def set_environment(self, environment: AssistantEnvironment) -> None:
        """实际渠道在会话启动后才确定，替换整个快照避免逐字段半更新。"""
        self._environment = environment

    def generation_context(self, chat_ctx: llm.ChatContext) -> llm.ChatContext:
        """协议与最新时钟只放入本次生成副本，净历史不缓存指令或失效能力。"""
        context = chat_ctx.copy()
        context.add_message(
            role="system",
            content=DELIVERY_INSTRUCTIONS
            + "\n"
            + self._environment.instructions(self._clock.now()),
        )
        return context

    @property
    def delivery_snapshot(self) -> DeliveryState | None:
        """只读取已实际输出的不可变状态；关闭或尚未装配时由共享入口返回中性就绪快照。"""
        if self._delivery_closed or self._delivery is None:
            return None
        return self._delivery.snapshot

    async def enable_delivery(self, endpoint: DeliverySnapshotEndpoint) -> None:
        """唯一所有者借出快照；重复或关闭后的迟到调用不能重新创建表现资源。"""
        if not endpoint.registered:
            raise RuntimeError("不能附加已关闭的表现快照")
        if (
            self._delivery_closed
            or self._delivery is not None
            or not self._room.isconnected()
            or self.session.output.audio is None
        ):
            return
        self._delivery = DeliveryTextOutput(self._room, snapshot_endpoint=endpoint)
        self._synchronizer = TranscriptSynchronizer(
            next_in_chain_audio=self.session.output.audio,
            next_in_chain_text=self._delivery,
        )
        self.session.output.audio = self._synchronizer.audio_output
        self.session.output.transcription = self._synchronizer.text_output

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterator[llm.ChatChunk | str | FlushSentinel]:
        """净历史不含控制头，复制上下文临时重申协议；不增加调用或污染历史与工具链。"""
        parser = DeliveryStreamParser()
        generation_context = self.generation_context(chat_ctx)
        source = Agent.default.llm_node(self, generation_context, tools, model_settings)
        async for chunk in source:
            if isinstance(chunk, str):
                segments = parser.feed(chunk)
            elif isinstance(chunk, FlushSentinel):
                segments = parser.finish()
            else:
                delta = chunk.delta
                segments = parser.feed(delta.content) if delta and delta.content else []
                if delta and delta.tool_calls:
                    segments.extend(parser.finish())
                if delta is None or chunk.usage or delta.tool_calls or delta.extra:
                    for segment in segments:
                        yield segment.intent.marker + segment.text
                        yield FlushSentinel()
                    yield chunk.model_copy(
                        update={
                            "delta": delta.model_copy(update={"content": None}) if delta else None
                        }
                    )
                    continue
            for segment in segments:
                yield segment.intent.marker + segment.text
                yield FlushSentinel()
        for segment in parser.finish():
            yield segment.intent.marker + segment.text
            yield FlushSentinel()

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterator[rtc.AudioFrame]:
        """节点只合成正文；自然回退由本地服务唯一负责，防止跨层重复重试。"""
        parser = DeliveryStreamParser()
        async for chunk in text:
            for segment in parser.feed(str(chunk)):
                async for frame in self._synthesize(segment):
                    yield frame
        for segment in parser.finish():
            async for frame in self._synthesize(segment):
                yield frame

    async def _synthesize(self, segment: SpeechSegment) -> AsyncIterator[rtc.AudioFrame]:
        """SDK 和 Agent 均不重放；本地服务独占一次回退预算，取消由流上下文回收。"""
        preset = segment.intent.voice_preset
        spoken = "".join(
            [
                part
                async for part in text_transforms.filter_emoji(
                    text_transforms.filter_markdown(_plain_body(segment.text))
                )
            ]
        )
        if not any(character.isalnum() for character in spoken):
            return
        options = replace(self.session.conn_options.tts_conn_options, max_retry=0)
        async with self._voices[preset].synthesize(spoken, conn_options=options) as stream:
            async for event in stream:
                yield event.frame

    async def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterator[str]:
        """每次提交都复核播放身份；符号保留正文但不占用首个可朗读句段的表现绑定。"""
        handle = self.session.current_speech
        if self._synchronizer is not None:
            await self._synchronizer.barrier()
        parser = DeliveryStreamParser()
        bound = False
        if handle is not None and handle.id not in self._speech_ids:
            self._speech_ids.add(handle.id)
            handle.add_done_callback(self._speech_done)
        try:
            async for chunk in text:
                for segment in parser.feed(str(chunk)):
                    if not bound and any(character.isalnum() for character in segment.text):
                        if not await self._bind(handle, segment.intent):
                            return
                        bound = True
                    if not self._can_transcribe(handle):
                        return
                    yield segment.text
            for segment in parser.finish():
                if not bound and any(character.isalnum() for character in segment.text):
                    if not await self._bind(handle, segment.intent):
                        return
                    bound = True
                if not self._can_transcribe(handle):
                    return
                yield segment.text
        finally:
            if handle is not None and handle.interrupted and self._delivery is not None:
                await self._delivery.cancel_reply(handle.id)

    async def _bind(self, handle, intent: DeliveryIntent) -> bool:
        """当前句段的不可变绑定不允许跨越播放许可或晚到的旧 speech handle。"""
        if not self._can_transcribe(handle):
            return False
        if self._delivery is None:
            return True
        await self._delivery.bind_segment(handle.id, intent)
        return True

    def _can_transcribe(self, handle) -> bool:
        """公开 handle 的许可可以在任一 await 后失效，不能沿用首段的布尔绑定结论。"""
        return (
            not self._delivery_closed
            and handle is not None
            and self.session.current_speech is handle
            and not handle.interrupted
            and not handle.done()
        )

    def _speech_done(self, handle) -> None:
        """完成回调带原回复身份，旧回调不能关闭后来获得播放许可的新回复。"""
        self._speech_ids.discard(handle.id)
        if self._delivery is None or not handle.interrupted:
            return
        task = asyncio.create_task(self._delivery.cancel_reply(handle.id))
        self._tasks.add(task)
        task.add_done_callback(self._observe_task)

    def _observe_task(self, task: asyncio.Task) -> None:
        """装饰输出的回收任务必须受监督，网络故障只取消表现而不扩散到通话。"""
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("人物表现状态回收失败")

    async def aclose(self) -> None:
        """关闭先阻止晚回调重开，再回收同步器和装饰输出；共享入口始终由会话装配者负责。"""
        self._delivery_closed = True
        synchronizer, delivery = self._synchronizer, self._delivery
        self._synchronizer = None
        self._delivery = None
        try:
            async with AsyncExitStack() as cleanup:
                if delivery is not None:
                    cleanup.push_async_callback(delivery.aclose)
                if synchronizer is not None:
                    cleanup.push_async_callback(synchronizer.aclose)
        finally:
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


async def _plain_body(text: str) -> AsyncIterator[str]:
    """先剥离受控头才使用 SDK 的公开正文过滤器，保留原有 Markdown／emoji 朗读约定。"""
    yield text
