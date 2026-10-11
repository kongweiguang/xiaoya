"""公开节点直接传递正文；Jev 与合成并行，实际播放后才授予人物动作许可。"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace

from livekit import rtc
from livekit.agents import Agent, FlushSentinel, ModelSettings, llm, tts
from livekit.agents.voice.transcription import TranscriptSynchronizer, text_transforms

from xiaoya.application.assistant_tools import Clock
from xiaoya.application.presentation import PresentationController
from xiaoya.domain.assistant import AssistantEnvironment
from xiaoya.domain.delivery import DeliveryIntent, SpeechSegment
from xiaoya.infrastructure.assistant_tools import SystemClock
from xiaoya.infrastructure.delivery_output import (
    DeliverySnapshotEndpoint,
    DeliveryState,
    DeliveryTextOutput,
)
from xiaoya.infrastructure.speech_segments import SpeechSegmenter

logger = logging.getLogger(__name__)
VOICE_INSTRUCTIONS = {
    "happy": "开心自然地说话。",
    "gentle": "轻柔温和地说话。",
}


class ExpressiveAgent(Agent):
    """单一正文流保留工具元数据，人物意图由独立的 Jev 控制器提供。"""

    def __init__(
        self,
        *,
        instructions: str,
        tools: list,
        room: rtc.Room,
        voices: dict[str, tts.TTS],
        environment: AssistantEnvironment | None = None,
        clock: Clock | None = None,
        presentation: PresentationController | None = None,
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
        self._voice_intents: dict[tuple[str, str], deque[DeliveryIntent]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._presentation = presentation or PresentationController()

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """语音和文字入口共用精确用户轮次，钩子不 await 外部决策或阻塞模型回复。"""
        self._presentation.complete_turn(new_message.text_content or "")
        self._voice_intents.clear()
        self._timing("turn_completed")

    @property
    def environment(self) -> AssistantEnvironment:
        """不可变事实由装配入口更新，不从用户消息或旧历史推断运行能力。"""
        return self._environment

    def set_environment(self, environment: AssistantEnvironment) -> None:
        """实际渠道在会话启动后才确定，替换整个快照避免逐字段半更新。"""
        self._environment = environment

    def _timing(self, stage: str) -> None:
        """单调时钟只记录阶段与匿名轮次，不能把服务端首帧误称为浏览器实际出声。"""
        logger.info(
            "语音计时",
            extra={
                "stage": stage,
                "turn_id": self._presentation.turn_id,
                "monotonic": time.monotonic(),
            },
        )

    def generation_context(self, chat_ctx: llm.ChatContext) -> llm.ChatContext:
        """最新身份和时钟只放入本次副本，历史不缓存失效事实或人物控制指令。"""
        context = chat_ctx.copy()
        context.add_message(
            role="system",
            content=self._environment.instructions(self._clock.now()),
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
        self._delivery = DeliveryTextOutput(
            self._room, snapshot_endpoint=endpoint, presentation=self._presentation
        )
        self._synchronizer = TranscriptSynchronizer(
            next_in_chain_audio=self.session.output.audio,
            next_in_chain_text=self._delivery,
        )
        self.session.output.audio = self._synchronizer.audio_output
        self.session.output.transcription = self._synchronizer.text_output

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterator[llm.ChatChunk | str | FlushSentinel]:
        """完整短句直接交给 SDK 独立播放，语义边界同时承载 Jev 身份；工具元数据无损。"""
        segmenter = SpeechSegmenter()
        generation_context = self.generation_context(chat_ctx)
        source = Agent.default.llm_node(self, generation_context, tools, model_settings)
        first_segment = True
        async for chunk in source:
            if isinstance(chunk, str):
                segments = segmenter.feed(chunk)
            elif isinstance(chunk, FlushSentinel):
                segments = segmenter.finish()
            else:
                delta = chunk.delta
                segments = segmenter.feed(delta.content) if delta and delta.content else []
                if delta and delta.tool_calls:
                    segments.extend(segmenter.finish())
                if delta is None or chunk.usage or delta.tool_calls or delta.extra:
                    for segment in segments:
                        if first_segment:
                            self._timing("first_semantic_segment")
                            first_segment = False
                        self._presentation.prepare_reply(segment.text)
                        yield segment.text
                        yield FlushSentinel()
                    yield chunk.model_copy(
                        update={
                            "delta": delta.model_copy(update={"content": None}) if delta else None
                        }
                    )
                    continue
            for segment in segments:
                if first_segment:
                    self._timing("first_semantic_segment")
                    first_segment = False
                self._presentation.prepare_reply(segment.text)
                yield segment.text
                yield FlushSentinel()
        for segment in segmenter.finish():
            if first_segment:
                self._timing("first_semantic_segment")
                first_segment = False
            self._presentation.prepare_reply(segment.text)
            yield segment.text
            yield FlushSentinel()

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterator[rtc.AudioFrame]:
        """完整短句立即合成，不等待 Jev；自然回退由本地服务独占，防止重复发声。"""
        parser = SpeechSegmenter()
        async for chunk in text:
            for segment in parser.feed(str(chunk)):
                async for frame in self._synthesize(segment):
                    yield frame
        for segment in parser.finish():
            async for frame in self._synthesize(segment):
                yield frame

    async def _synthesize(self, segment: SpeechSegment) -> AsyncIterator[rtc.AudioFrame]:
        """每段合成采样已获得的 Jev 语气，HTTP 开始后固定；预生成不借用当前旧 handle。"""
        self._presentation.prepare_reply(segment.text)
        intent = self._presentation.voice_for(segment.text)
        preset = intent.voice_preset
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
        self._voice_intents.setdefault((self._presentation.turn_id, segment.text), deque()).append(
            intent
        )
        options = replace(self.session.conn_options.tts_conn_options, max_retry=0)
        self._timing("tts_requested")
        first_frame = True
        async with self._voices[preset].synthesize(spoken, conn_options=options) as stream:
            async for event in stream:
                if first_frame:
                    self._timing("tts_first_frame")
                    first_frame = False
                yield event.frame

    async def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterator[str]:
        """每次提交都复核播放身份；符号保留正文但不占用首个可朗读句段的表现绑定。"""
        handle = self.session.current_speech
        if self._synchronizer is not None:
            await self._synchronizer.barrier()
        parser = SpeechSegmenter()
        bound = False
        if handle is not None and handle.id not in self._speech_ids:
            self._speech_ids.add(handle.id)
            handle.add_done_callback(self._speech_done)
        try:
            async for chunk in text:
                for segment in parser.feed(str(chunk)):
                    if not bound and any(character.isalnum() for character in segment.text):
                        if not await self._bind(handle, segment.text):
                            return
                        bound = True
                    if not self._can_transcribe(handle):
                        return
                    yield segment.text
            for segment in parser.finish():
                if not bound and any(character.isalnum() for character in segment.text):
                    if not await self._bind(handle, segment.text):
                        return
                    bound = True
                if not self._can_transcribe(handle):
                    return
                yield segment.text
        finally:
            if handle is not None and handle.interrupted and self._delivery is not None:
                await self._delivery.cancel_reply(handle.id)

    async def _bind(self, handle, text: str) -> bool:
        """绑定记录已冻结声音，视觉只由 Jev 决定；每次 await 前复核当前回复许可。"""
        if not self._can_transcribe(handle):
            return False
        if self._delivery is None:
            return True
        await self._delivery.bind_segment(
            handle.id,
            self._committed_voice(text),
            text,
        )
        return True

    def _committed_voice(self, text: str) -> DeliveryIntent:
        """相同句子也按合成顺序消费声音事实，预生成下一段不能覆盖上一段的冻结风格。"""
        key = (self._presentation.turn_id, text)
        intents = self._voice_intents.get(key)
        if not intents:
            return DeliveryIntent()
        intent = intents.popleft()
        if not intents:
            del self._voice_intents[key]
        return intent

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
        """正常done才完成音频许可，文字flush不算播放结束；打断仍立即撤销精确旧回复。"""
        self._speech_ids.discard(handle.id)
        if handle.interrupted:
            self._timing("speech_interrupted_done")
        if self._delivery is None:
            return
        task = asyncio.create_task(
            self._delivery.cancel_reply(handle.id)
            if handle.interrupted
            else self._delivery.finish_reply(handle.id)
        )
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
        self._voice_intents.clear()
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
    """SDK 公开过滤器只在实际合成边界净化朗读，字幕与历史仍保留原文。"""
    yield text
