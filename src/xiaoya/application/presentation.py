"""每个会话的唯一表现调度：模型判断并行执行，更新只消费最新语义上下文。"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationCue,
    PresentationDecision,
    PresentationPhase,
    baseline_cue,
    choose_presentation,
)

_LOGGER = logging.getLogger(__name__)


class PresentationDecider(Protocol):
    """供应商仅返回有限意图，失败与关闭都通过端口表达，应用层不认识 HTTP。"""

    async def decide(self, context: PresentationContext) -> PresentationDecision | None:
        """不可靠外部判断可放弃，主会话不因此等待或失败。"""
        ...

    async def aclose(self) -> None:
        """会话结束释放自己的连接池，不影响其他房间。"""
        ...


class PresentationController:
    """单个 worker 和最新槽位替代请求堆积，取消后永远不重新采用旧结果。"""

    def __init__(self, decider: PresentationDecider | None = None) -> None:
        """空端口仍运行同一表现规则，只省略外部判断，避免产生第二种运行模式。"""
        self._decider = decider
        self._output: Callable[[PresentationCue], None] | None = None
        self._context = PresentationContext("idle", uuid4().hex, 0)
        self._pending: tuple[PresentationContext, bool] | None = None
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._user_text = ""
        self._cue = PresentationCue()
        self._accepted: tuple[PresentationContext, PresentationDecision] | None = None
        self._inflight: PresentationContext | None = None
        self._preview: PresentationContext | None = None

    @property
    def turn_id(self) -> str:
        """绑定捕获当前轮次，使积压运输操作无法越过新用户开口的取消边界。"""
        return self._context.turn_id

    def voice_for(self, text: str) -> DeliveryIntent:
        """声音只采纳同轮次精确正文的判断；未判定下一句不能沿用上一句的开心或悲伤。"""
        accepted = self._accepted
        if (
            accepted
            and (accepted[0].reply_preview or accepted[0].phase == "speaking")
            and accepted[0].turn_id == self.turn_id
            and accepted[0].text == text
            and accepted[0].user_text == self._user_text
            and accepted[1].style_confidence >= 0.65
        ):
            return DeliveryIntent(accepted[1].intent.style)
        return DeliveryIntent()

    def attach(self, output: Callable[[PresentationCue], None]) -> None:
        """运输适配器只借用意图回调，网络排队不能反向阻塞应用状态更新。"""
        if not self._closed:
            self._output = output

    def phase(self, phase: PresentationPhase) -> None:
        """每次用户开口建立新轮次；思考事件不能提前让预生成片段取得播放许可。"""
        if self._closed or phase == self._context.phase:
            return
        turn_id = self._context.turn_id
        if phase == "listening":
            turn_id = uuid4().hex
            self._user_text = ""
        self._accepted = None
        self._preview = None
        self._context = PresentationContext(
            phase,
            turn_id,
            self._context.revision + 1,
            text=self._user_text,
            user_text=self._user_text,
        )
        self._pending = None
        self._emit(baseline_cue(self._context))

    def complete_turn(self, text: str) -> None:
        """文字输入也获得独立轮次；完成钩子只提交异步工作，绝不延迟 LLM 启动。"""
        if self._closed:
            return
        turn_id = self._context.turn_id if self._context.phase == "listening" else uuid4().hex
        self._user_text = text.strip()
        self._accepted = None
        self._preview = None
        self._context = PresentationContext(
            "thinking",
            turn_id,
            self._context.revision + 1,
            text=self._user_text,
            user_text=self._user_text,
        )
        self._emit(baseline_cue(self._context))
        self._request(True)

    def transcribe(self, text: str, *, final: bool) -> None:
        """识别修订替换整个输入，最终文本立即唤醒；真实用户轮次结束后不再倾听表演。"""
        if self._closed or not text.strip():
            return
        if self._context.phase == "idle":
            self.phase("listening")
        self._user_text = text.strip()
        self._context = replace(
            self._context,
            revision=self._context.revision + 1,
            text=self._context.text
            if self._context.reply_preview or self._context.phase == "speaking"
            else self._user_text,
            user_text=self._user_text,
        )
        self._accepted = None
        self._preview = None
        if self._context.phase in {"listening", "thinking"}:
            self._request(final)

    def prepare_reply(self, text: str, *, final: bool = True) -> None:
        """纯正文可在合成前并行判断；预生成只改变微表情，动作必须等实际播放许可。"""
        if self._closed or self._context.phase == "listening" or not text.strip():
            return
        if (self._preview and self._preview.text == text) or (
            self._context.phase == "speaking" and self._context.text == text
        ):
            return
        preview = PresentationContext(
            "thinking",
            self._context.turn_id,
            self._context.revision + 1,
            text=text,
            user_text=self._user_text,
            reply_preview=True,
        )
        self._preview = preview
        if self._context.phase != "speaking":
            self._context = preview
        self._request(final, preview)

    def speak(
        self, reply_id: str, segment_id: str, intent: DeliveryIntent, text: str
    ) -> PresentationCue:
        """精确正文预判可在实际出声时采用；迟到或其他正文判断不能越过播放身份。"""
        if self._closed:
            return self._cue
        accepted = self._accepted
        self._context = PresentationContext(
            "speaking",
            self._context.turn_id,
            self._context.revision + 1,
            text,
            self._user_text,
            reply_id,
            segment_id,
            intent.style,
        )
        if (
            accepted
            and (
                accepted[0].reply_preview
                or (accepted[0].reply_id, accepted[0].segment_id) == (reply_id, segment_id)
            )
            and accepted[0].turn_id == self.turn_id
            and accepted[0].text == text
            and accepted[0].user_text == self._user_text
        ):
            cue = choose_presentation(self._context, accepted[1])
            self._accepted = (self._context, accepted[1])
        else:
            cue = baseline_cue(self._context)
            if not self._preview_matches(self._inflight):
                self._request(True)
        self._cue = cue
        if self._preview and self._preview.text == text:
            self._preview = None
        return cue

    def _preview_matches(self, context: PresentationContext | None) -> bool:
        """预判只能晋升到同轮次、完全相同正文的当前播放身份，不能授权其他片段。"""
        return bool(
            context
            and context.reply_preview
            and self._context.phase == "speaking"
            and context.turn_id == self._context.turn_id
            and context.text == self._context.text
            and context.user_text == self._context.user_text
        )

    def finish(self, reply_id: str, segment_id: str) -> PresentationCue | None:
        """迟到关闭只作废自己的片段，不能收束下一回复或新的倾听轮次。"""
        if not self._closed and (reply_id, segment_id) == (
            self._context.reply_id,
            self._context.segment_id,
        ):
            self._context = PresentationContext(
                "idle", self._context.turn_id, self._context.revision + 1
            )
            if self._pending is not None and self._pending[0] != self._preview:
                self._pending = None
            if self._accepted is not None and not self._accepted[0].reply_preview:
                self._accepted = None
            self._cue = baseline_cue(self._context)
            return self._cue
        return None

    def _emit(self, cue: PresentationCue) -> None:
        """运输失败只能丢失装饰，不传播进模型事件回调或语音任务。"""
        if self._closed:
            return
        self._cue = cue
        if self._output is not None:
            try:
                self._output(cue)
            except Exception as error:
                _LOGGER.warning("表现运输已降级（%s）", type(error).__name__)

    def _request(self, final: bool, context: PresentationContext | None = None) -> None:
        """最新槽位覆盖积压内容，任务仅在实际需要外部判断时创建。"""
        if self._closed or self._decider is None:
            return
        self._pending = (context or self._context, final)
        self._wake.set()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="xiaoya-presentation")

    async def _run(self) -> None:
        """取消优先传播；每次 await 后复核完整上下文，过期判断不会形成回放。"""
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._pending is None:
                continue
            context, final = self._pending
            if not final:
                delay = 0.6 if context.phase == "listening" else 0.45
                deadline = asyncio.get_running_loop().time() + delay
                while self._pending is not None and not self._pending[1]:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), remaining)
                    except TimeoutError:
                        break
            if self._pending is None:
                continue
            context, _ = self._pending
            self._pending = None
            self._inflight = context
            try:
                async with asyncio.timeout(0.7 if context.phase == "listening" else 0.9):
                    decision = await self._decider.decide(context)
            except Exception as error:
                _LOGGER.warning("表现增强已降级（%s）", type(error).__name__)
                continue
            finally:
                self._inflight = None
            if decision is None or self._closed:
                continue
            if context == self._context or self._preview_matches(context):
                self._accepted = (self._context, decision)
                self._emit(choose_presentation(self._context, decision))
            elif (
                context == self._preview
                and context.turn_id == self.turn_id
                and context.user_text == self._user_text
            ):
                # 仅缓存即将播放的正文；当前动作、表情和播放身份均不受未来段判断影响。
                self._accepted = (context, decision)

    async def aclose(self) -> None:
        """先撤销回调和积压，再取消唯一任务，最后释放端口的网络资源。"""
        if self._closed:
            return
        self._closed = True
        self._context = PresentationContext("idle", self.turn_id, self._context.revision + 1)
        self._cue = baseline_cue(self._context)
        self._accepted = None
        self._preview = None
        self._output = None
        self._pending = None
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        if self._decider is not None:
            await self._decider.aclose()
