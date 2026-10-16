"""每个会话的唯一表现调度：模型判断并行执行，更新只消费最新语义上下文。"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol
from uuid import uuid4

from xiaoya.domain.assistant import command_response_instructions
from xiaoya.domain.delivery import GESTURES, DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationCue,
    PresentationDecision,
    PresentationPhase,
    UserPresentationDecision,
    baseline_cue,
    choose_presentation,
    choose_user_command,
    choose_user_prohibition,
    natural_gesture_ready_at,
)
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _NaturalOffer:
    """已下发目录区别于实际执行事实；迟回只能记自己的账，不能重新取得播放许可。"""

    cue: PresentationCue
    started_at: float | None = None
    terminal: str = ""
    revoked: bool = False
    unknown_until: float = 0.0


class PresentationDecider(Protocol):
    """供应商仅返回有限意图，失败与关闭都通过端口表达，应用层不认识 HTTP。"""

    async def decide(self, context: PresentationContext) -> PresentationDecision | None:
        """不可靠外部判断可放弃，主会话不因此等待或失败。"""
        ...

    async def decide_user(self, context: PresentationContext) -> UserPresentationDecision | None:
        """用户请求独立于回复正文，才能在声音受阻时仍确认指令及禁止范围。"""
        ...

    async def warmup(self) -> None:
        """会话可提前准备免费连接，不能查询用户正文、自动付费或阻塞语音启动。"""
        ...

    async def aclose(self) -> None:
        """会话结束释放自己的连接池，不影响其他房间。"""
        ...


class PresentationController:
    """单个 worker 和最新槽位替代请求堆积，取消后永远不重新采用旧结果。"""

    def __init__(self, decider: PresentationDecider | None = None) -> None:
        """查询槽和执行账分开；自然候选仅占当前槽，真实回执预算不随用户新轮清零。"""
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
        self._decision_task: asyncio.Task[PresentationDecision | None] | None = None
        self._preview: PresentationContext | None = None
        self._next_preview: PresentationContext | None = None
        self._playing_preview: PresentationContext | None = None
        self._failed_preview: PresentationContext | None = None
        self._failed_current: PresentationContext | None = None
        self._user_task: asyncio.Task[None] | None = None
        self._owned_tasks: set[asyncio.Task[None]] = set()
        self._user_identity: tuple[str, str] | None = None
        self._user_started = False
        self._user_task_complete = False
        self._user_result: UserPresentationDecision | None = None
        self._user_result_ready = False
        self._user_revision = 0
        self._user_final = False
        self._user_speaking = False
        self._transcript_final = False
        self._prohibited = "all"
        self._command: tuple[str, str, float, str] | None = None
        self._command_turn = False
        self._command_watch: asyncio.Task[None] | None = None
        self._feedback = "none"
        self._last_command = "none"
        self._execution_state = "none"
        self._prepare_task: asyncio.Task[None] | None = None
        self._receipts: dict[str, str] = {}
        self._recent_user = ""
        self._recent_assistant = ""
        self._completed_text: list[str] = []
        self._played_reply_id = ""
        self._played_turn_id = ""
        self._played_user_text = ""
        self._played_segments: set[str] = set()
        self._base = self._cue
        self._last_expression: str | None = None
        self._reply_expression_adopted = False
        self._expression_finished = False
        self._natural_pending: PresentationCue | None = None
        self._natural_timer: asyncio.TimerHandle | None = None
        self._natural_offers: dict[tuple[str, str], _NaturalOffer] = {}
        self._natural_active: tuple[str, str] | None = None
        self._natural_starts: list[tuple[str, float]] = []
        self._natural_unknown_until = 0.0
        self._natural_pruned_unknown_until = 0.0

    @property
    def turn_id(self) -> str:
        """绑定捕获当前轮次，使积压运输操作无法越过新用户开口的取消边界。"""
        return self._context.turn_id

    def voice_for(self, text: str) -> DeliveryIntent:
        """提交前预判仅缓存；声音只在提交后借精确同轮正文，不能提前采纳或沿用下一句。"""
        if self._readonly_reply_permitted():
            return DeliveryIntent()
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

    def command_context(self) -> str:
        """只组装本轮受控事实及相应答句边界，待判断不预列失败，目标和完成历史仍分开。"""
        response_instructions = command_response_instructions(self._execution_state)
        command = self._command
        target = "尚未确认" if self._execution_state == "pending" else "无待执行目标"
        if (
            command is not None
            and command[0].startswith(f"{self.turn_id}:command:")
            and command[1] in GESTURES - {"none"}
        ):
            target = command[1]
        return (
            "动作范围只看当前一条用户消息；历史动作不属于本次请求，不能与当前动作合并。"
            f"本次动作状态：{self._execution_state}。"
            f"本轮仍在执行或待执行的动作目标：{target}。"
            "无待执行目标不否认completed事实。"
            "已确认单一目标时只回应本条请求，"
            "不添加上轮动作，也不虚构需要二选一。"
            f"仅供当前明确再做一次引用的历史记录：最近一次客户端确认完成的动作：{self._last_command}。"
            "再做一次：有记录沿用且不再追问；none则问对象。"
            + response_instructions
            + "普通聊天只说口语。"
        )

    def prepare(self) -> None:
        """连接准备独立持有且重复调用幂等，启动和首声不等待慢代理握手。"""
        if self._closed or self._decider is None or self._prepare_task is not None:
            return
        self._prepare_task = asyncio.create_task(
            self._prepare(), name="xiaoya-presentation-connect"
        )
        self._owned_tasks.add(self._prepare_task)
        self._prepare_task.add_done_callback(self._owned_tasks.discard)

    async def _prepare(self) -> None:
        """握手失败不伪装会话启动失败，完整输入仍会获得自己的有限判断和可见反馈。"""
        try:
            await self._decider.warmup()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning("表现连接准备降级（%s）", type(error).__name__)

    def _new_turn(self) -> None:
        """新轮撤旧候选和查询；已开始及未知执行的会话节奏不能用新turn绕过。"""
        self._clear_natural_pending()
        self._revoke_natural_offer()
        if self._user_task is not None:
            self._user_task.cancel()
        if self._decision_task is not None:
            self._decision_task.cancel()
        self._end_command("canceled", emit=False)
        self._user_identity = None
        self._user_revision += 1
        self._user_final = False
        self._user_speaking = False
        self._transcript_final = False
        self._user_task_complete = False
        self._user_result = None
        self._user_result_ready = False
        self._prohibited = "all"
        self._command_turn = False
        self._feedback = "none"
        self._execution_state = "none"
        self._completed_text.clear()
        self._played_reply_id = ""
        self._played_turn_id = ""
        self._played_segments.clear()
        self._accepted = None
        self._preview = self._next_preview = None
        self._playing_preview = self._failed_preview = self._failed_current = None
        self._last_expression = None
        self._reply_expression_adopted = False
        self._expression_finished = False

    def phase(self, phase: PresentationPhase) -> None:
        """提交前thinking保留最终输入的只读两槽；待机或新轮撤其代次，阶段不授权采纳。"""
        if self._closed or phase == self._context.phase:
            return
        preserve_preview = phase == "thinking" and self._readonly_reply_permitted()
        turn_id = self._context.turn_id
        if phase == "idle":
            self._collect_finished_segment()
            self._release_playing_judgment()
        if phase == "listening":
            self._new_turn()
            turn_id = uuid4().hex
            self._user_text = ""
        if not preserve_preview:
            self._discard_uncommitted_reply()
            self._accepted = None
            self._preview = None
            self._failed_preview = None
        self._context = PresentationContext(
            phase,
            turn_id,
            self._context.revision + 1,
            text=self._user_text,
            user_text=self._user_text,
        )
        if not preserve_preview:
            self._pending = None
        self._emit(self._current_cue(self._context))

    def _readonly_reply_permitted(self) -> bool:
        """最终ASR与当前完整查询身份一致才可预判；这不是提交或执行许可。"""
        return (
            not self._closed
            and not self._user_final
            and self._transcript_final
            and self._context.phase in {"listening", "thinking"}
            and not self._context.reply_preview
            and self._user_task_complete
            and self._user_task is not None
            and self._user_identity == (self.turn_id, self._user_text)
        )

    def _discard_uncommitted_reply(self) -> None:
        """撤最终输入的只读槽并推进代次；吞取消的同文旧结果也不能匹配重新建立的槽。"""
        if self._user_final or (self._preview is None and self._next_preview is None):
            return
        if (
            self._inflight in (self._preview, self._next_preview)
            and self._decision_task is not None
        ):
            self._decision_task.cancel()
        self._context = replace(self._context, revision=self._context.revision + 1)
        self._preview = self._next_preview = None
        self._playing_preview = self._failed_preview = self._failed_current = None
        self._accepted = None
        if self._pending is not None and self._pending[0].reply_preview:
            self._pending = None

    def user_speaking(self, active: bool) -> None:
        """公开VAD只控制倾听动作许可；同轮停顿恢复不重建轮次，也不把停声当命令提交。"""
        if self._closed:
            return
        if active and self._context.phase != "listening":
            self.phase("listening")
        if active == self._user_speaking:
            return
        self._user_speaking = active
        self._invalidate_listening_request()
        if self._context.phase == "listening":
            self._emit(self._current_cue(self._context))

    def _invalidate_listening_request(self, *, include_prefetch: bool = False) -> None:
        """正常停声保留最终预取及回复两槽；partial或空final撤代次，已提交命令仍独立。"""
        if include_prefetch:
            self._discard_uncommitted_reply()
        if self._user_task_complete and (self._user_final or not include_prefetch):
            return
        self._user_revision += 1
        self._user_identity = None
        self._user_task_complete = False
        self._user_result = None
        self._user_result_ready = False
        task, self._user_task = self._user_task, None
        if task is not None:
            task.cancel()

    def complete_turn(self, text: str) -> None:
        """精确最终输入提交才保留只读两槽及其失败；原位晋升不重发或重置已有预算。"""
        if self._closed:
            return
        text = text.strip()
        prefetched = (
            not self._user_final
            and self._context.phase in {"listening", "thinking"}
            and not self._context.reply_preview
            and self._user_task_complete
            and self._user_identity == (self.turn_id, text)
            and self._user_task is not None
        )
        preserve_preview = prefetched and self._readonly_reply_permitted()
        if not preserve_preview:
            self._discard_uncommitted_reply()
        if self._context.phase != "listening" and not prefetched:
            self._new_turn()
        turn_id = (
            self._context.turn_id
            if self._context.phase == "listening" or prefetched
            else uuid4().hex
        )
        self._user_text = text
        self._user_final = True
        self._user_speaking = False
        self._transcript_final = True
        if not preserve_preview:
            self._accepted = None
            self._preview = None
        self._prohibited = "all"
        self._feedback = "none"
        self._execution_state = "pending"
        self._context = PresentationContext(
            "thinking",
            turn_id,
            self._context.revision + 1,
            text=self._user_text,
            user_text=self._user_text,
        )
        self._emit(self._current_cue(self._context))
        self._request_user(complete=True)

    def transcribe(self, text: str, *, final: bool) -> None:
        """final立即预取且重复全文共享；改文或恢复partial撤回复槽，采纳仍等待真实提交。"""
        if self._closed:
            return
        if not text.strip():
            if final:
                self._transcript_final = True
                self._invalidate_listening_request(include_prefetch=True)
                if self._context.phase == "listening":
                    self._emit(self._current_cue(self._context))
            return
        if self._context.phase == "idle":
            self.phase("listening")
        if final != self._transcript_final:
            self._transcript_final = final
            self._invalidate_listening_request(include_prefetch=not final)
        changed = self._user_text != text.strip()
        if changed:
            self._discard_uncommitted_reply()
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
            self._reply_expression_adopted = False
        if final and self._context.phase == "listening":
            self._emit(self._current_cue(self._context))
        if changed and self._preview is not None:
            self._preview = (
                self._context
                if self._context.reply_preview
                else replace(self._preview, user_text=self._user_text)
            )
        if self._context.phase in {"listening", "thinking"}:
            self._request_user(complete=self._user_final or final)
        if changed and (self._context.reply_preview or self._context.phase == "speaking"):
            self._request(True)

    def prepare_reply(self, text: str, *, final: bool = True) -> None:
        """完整final可在SDK提交前只读预判首句；沿用两槽，提交及真实播放分别授予采纳许可。"""
        readonly = self._readonly_reply_permitted()
        if self._closed or self._context.phase == "listening" and not readonly or not text.strip():
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
            recent_user_text=self._recent_user,
            recent_assistant_text=self._recent_assistant,
            last_command=self._last_command,
        )
        if self._preview is not None and self._preview.text != text:
            if self._next_preview is None:
                self._next_preview = preview
            return
        self._preview = preview
        if self._context.phase != "speaking" and not readonly:
            self._context = preview
        self._request(final, preview)

    def speak(
        self, reply_id: str, segment_id: str, intent: DeliveryIntent, text: str
    ) -> PresentationCue:
        """精确播放才晋升预判；同步SDK头也共用自然节奏装饰，不能绕过异步_emit入口。"""
        if self._closed or (self._expression_finished and reply_id == self._played_reply_id):
            return self._cue
        self._discard_uncommitted_reply()
        accepted = self._accepted
        previous = self._context
        if self._expression_finished and reply_id != self._played_reply_id:
            self._expression_finished = False
        self._context = PresentationContext(
            "speaking",
            self._context.turn_id,
            self._context.revision + 1,
            text,
            self._user_text,
            reply_id,
            segment_id,
            intent.style,
            recent_user_text=self._recent_user,
            recent_assistant_text=self._recent_assistant,
            last_command=self._last_command,
        )
        if not self._same_segment(previous, self._context):
            self._playing_preview = None
            self._failed_current = None
        if self._preview_matches(self._preview):
            self._playing_preview = self._preview
        elif self._playing_preview is None and self._preview_matches(self._inflight):
            self._playing_preview = self._inflight
        if self._failed_preview is not None and self._failed_preview == self._playing_preview:
            self._failed_current = self._context
            self._failed_preview = None
        failed = self._same_segment(self._failed_current, self._context)
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
            cue = self._current_cue(self._context, accepted[1])
            self._accepted = (self._context, accepted[1])
        else:
            cue = self._current_cue(self._context)
            if not failed and not self._preview_matches(self._inflight):
                self._request(True)
        self._base = cue
        self._cue = self._decorate(cue)
        if self._preview and self._preview.text == text:
            self._preview, self._next_preview = self._next_preview, None
        if self._preview is not None and (
            self._accepted is not None
            and self._accepted[0] == self._context
            or self._preview_matches(self._inflight)
            or failed
        ):
            self._queue_next_preview(self._context)
        return self._cue

    @staticmethod
    def _same_segment(left: PresentationContext | None, right: PresentationContext) -> bool:
        """同一真实身份和同一语义正文才共享失败事实；修订号变化不允许重复付款。"""
        return bool(
            left is not None
            and left.phase == right.phase == "speaking"
            and (left.turn_id, left.reply_id, left.segment_id, left.text, left.user_text)
            == (right.turn_id, right.reply_id, right.segment_id, right.text, right.user_text)
        )

    def _release_playing_judgment(self) -> None:
        """只取消绑定已结束当前身份的请求，不能因旧finish撤销合法下一预判或指令任务。"""
        current, inflight = self._context, self._inflight
        if current.phase == "speaking" and inflight is not None:
            same_id = inflight.phase == "speaking" and (
                inflight.turn_id,
                inflight.reply_id,
                inflight.segment_id,
            ) == (current.turn_id, current.reply_id, current.segment_id)
            if (same_id or inflight == self._playing_preview) and self._decision_task is not None:
                self._decision_task.cancel()
        self._playing_preview = self._failed_current = None

    def _preview_matches(self, context: PresentationContext | None) -> bool:
        """精确当前槽或已晋升槽才可采纳；同文旧代次、邻句、旧轮和待机都不借用。"""
        return bool(
            context
            and context.reply_preview
            and context in (self._preview, self._playing_preview)
            and self._context.phase == "speaking"
            and context.turn_id == self._context.turn_id
            and context.text == self._context.text
            and context.user_text == self._context.user_text
        )

    def finish(self, reply_id: str, segment_id: str) -> PresentationCue | None:
        """结束精确当前句即撤其冷却候选和判断，下一句不得借旧动作ID或旧timer补演。"""
        if not self._closed and (reply_id, segment_id) == (
            self._context.reply_id,
            self._context.segment_id,
        ):
            self._collect_finished_segment()
            self._release_playing_judgment()
            self._context = PresentationContext(
                "idle", self._context.turn_id, self._context.revision + 1
            )
            if self._pending is not None and self._pending[0] != self._preview:
                self._pending = None
            if self._accepted is not None and not self._accepted[0].reply_preview:
                self._accepted = None
            self._base = replace(
                baseline_cue(self._context),
                intent=DeliveryIntent(self._base.intent.style),
                energy=self._base.energy,
            )
            self._cue = self._decorate(self._base)
            return self._cue
        return None

    def complete_reply(self, reply_id: str) -> None:
        """正常完成清理当前失败事实；新预判可留待起音，指令始终独立于语音终结。"""
        if not self._closed and self._context.reply_id == reply_id:
            self._collect_finished_segment()
        if (
            not self._closed
            and self._played_reply_id == reply_id
            and self._played_turn_id == self.turn_id
            and (not self._context.reply_id or self._context.reply_id == reply_id)
            and self._completed_text
        ):
            self._recent_user = self._played_user_text
            self._recent_assistant = "".join(self._completed_text)[-1200:]
            self._last_expression = None
            self._expression_finished = True
            # 未绑定的新回复不能被旧 handle 的 done 清空；旧身份仍由 speak 的完成门封住。
            future = self._context if self._context.reply_preview else None
            if not future:
                self._accepted = None
                self._preview = self._next_preview = None
                self._pending = None
                self._failed_preview = None
            self._playing_preview = self._failed_current = None
            if self._decision_task is not None and self._inflight != future:
                self._decision_task.cancel()
            if self._context.reply_id == reply_id:
                self._context = PresentationContext(
                    "idle", self.turn_id, self._context.revision + 1
                )
            self._emit(self._current_cue(self._context))

    def _current_cue(
        self, context: PresentationContext, decision: PresentationDecision | None = None
    ) -> PresentationCue:
        """只有实际当前回复的高置信表情锁定优先级；未来缓存、低分和倾听不阻止用户兜底。"""
        if self._expression_finished:
            decision = None
        elif decision is not None and decision.style_confidence >= 0.65:
            self._last_expression = decision.intent.style
            if context.reply_preview or context.phase == "speaking":
                self._reply_expression_adopted = True
        current = replace(context, expression_style=self._last_expression)
        cue = (
            choose_presentation(current, decision)
            if decision is not None
            else baseline_cue(current)
        )
        if context.phase == "listening" and (
            not self._user_speaking or self._transcript_final or self._user_final
        ):
            cue = replace(
                cue,
                intent=DeliveryIntent(cue.intent.style),
                gesture_id="",
                gesture_source="none",
            )
        return cue

    def _collect_finished_segment(self) -> None:
        """SDK先发idle再完成handle，视觉撤销前保留有界正文候选，正常done才能封存且不重计。"""
        context = self._context
        if context.phase != "speaking" or not context.reply_id or not context.segment_id:
            return
        if (self._played_turn_id, self._played_reply_id) != (context.turn_id, context.reply_id):
            self._completed_text.clear()
            self._played_segments.clear()
            self._played_turn_id = context.turn_id
            self._played_reply_id = context.reply_id
            self._played_user_text = self._user_text
        if context.segment_id not in self._played_segments:
            self._played_segments.add(context.segment_id)
            self._completed_text.append(context.text)

    def _emit(self, cue: PresentationCue) -> None:
        """表情立即输出而自然手势经统一装饰；运输及ACK均不能反向等待或阻塞声音。"""
        if self._closed:
            return
        self._base = cue
        decorated = self._decorate(cue)
        if decorated == self._cue:
            return
        self._cue = decorated
        if self._output is not None:
            try:
                self._output(self._cue)
            except Exception as error:
                _LOGGER.warning("表现运输已降级（%s）", type(error).__name__)

    def _decorate(self, cue: PresentationCue) -> PresentationCue:
        """命令保持独立许可；自然节奏最后统一装饰所有返回路径，不改口型和语义门槛。"""
        command = self._command
        if command is not None:
            cue = replace(
                cue,
                intent=DeliveryIntent(cue.intent.style, command[1]),
                gesture_id=command[0],
                gesture_source="command",
                energy=command[2],
                command_feedback=self._feedback,
            )
        elif self._command_turn or self._prohibited in {"all", cue.intent.gesture}:
            cue = replace(
                cue,
                intent=DeliveryIntent(cue.intent.style),
                gesture_id="",
                gesture_source="none",
            )
        return self._natural_cue(replace(cue, command_feedback=self._feedback))

    @staticmethod
    def _without_natural(cue: PresentationCue) -> PresentationCue:
        """冷却仅移除执行许可，Jev神态、能量与真实语音身份必须完整保留。"""
        return replace(
            cue,
            intent=DeliveryIntent(cue.intent.style),
            gesture_id="",
            gesture_source="none",
        )

    def _clear_natural_pending(self) -> None:
        """仅持有当前候选和一个timer；即使旧回调已排队，身份对象也会使其失效。"""
        timer, self._natural_timer = self._natural_timer, None
        if timer is not None:
            timer.cancel()
        self._natural_pending = None

    def _revoke_natural_offer(self) -> None:
        """撤许可不推断是否执行；未知隔离记到本offer，明确未发力终态只能释放自己的占用。"""
        identity, self._natural_active = self._natural_active, None
        offer = self._natural_offers.get(identity)
        if offer is None:
            return
        offer.revoked = True
        if offer.started_at is None and not offer.terminal:
            offer.unknown_until = max(offer.unknown_until, monotonic() + 10.0)
            self._refresh_natural_unknown()

    def _refresh_natural_unknown(self) -> None:
        """最大值由各身份独立持有；目录淘汰的未知保守留到原截止，不能被别人的false退款。"""
        self._natural_unknown_until = max(
            self._natural_pruned_unknown_until,
            max((offer.unknown_until for offer in self._natural_offers.values()), default=0.0),
        )

    def _natural_scope_current(self, cue: PresentationCue) -> bool:
        """许可绑定当前真实轮次和句段；倾听还必须保持VAD/partial，命令和禁止始终优先。"""
        context = self._context
        return (
            not self._closed
            and not context.reply_preview
            and (cue.phase, cue.turn_id, cue.reply_id, cue.segment_id)
            == (context.phase, context.turn_id, context.reply_id, context.segment_id)
            and cue.phase in {"listening", "speaking"}
            and (cue.phase != "listening" or self._listening_permitted())
            and self._command is None
            and not self._command_turn
            and self._prohibited not in {"all", cue.intent.gesture}
        )

    def _natural_cue(self, cue: PresentationCue) -> PresentationCue:
        """同回复同手势只安排一次，防逐句机械重复；未真正offer的预判和冷却不消耗此约束。"""
        self._clear_natural_pending()
        active = self._natural_offers.get(self._natural_active)
        if active is not None and (
            not self._natural_scope_current(active.cue)
            or active.cue.phase == "listening"
            and cue.gesture_source != "dialogue"
        ):
            self._revoke_natural_offer()
            active = None
        if cue.gesture_source != "dialogue":
            return cue
        if not self._natural_scope_current(cue):
            return self._without_natural(cue)
        identity = (cue.turn_id, cue.gesture_id)
        known = self._natural_offers.get(identity)
        if known is not None:
            if (
                not known.revoked
                and not known.terminal
                and self._natural_active == identity
                and (known.cue.phase, known.cue.reply_id, known.cue.segment_id)
                == (cue.phase, cue.reply_id, cue.segment_id)
            ):
                return cue
            return self._without_natural(cue)
        if (
            cue.phase == "speaking"
            and cue.reply_id
            and any(
                offer.cue.phase == "speaking"
                and (offer.cue.turn_id, offer.cue.reply_id, offer.cue.intent.gesture)
                == (cue.turn_id, cue.reply_id, cue.intent.gesture)
                for offer in self._natural_offers.values()
            )
        ):
            return self._without_natural(cue)
        now = monotonic()
        ready_at = max(
            self._natural_unknown_until,
            natural_gesture_ready_at(cue.intent.gesture, tuple(self._natural_starts), now),
        )
        if active is not None or ready_at > now:
            self._natural_pending = cue
            if active is None:
                self._natural_timer = asyncio.get_running_loop().call_later(
                    ready_at - now, self._resume_natural, cue
                )
            return self._without_natural(cue)
        self._natural_offers[identity] = _NaturalOffer(cue)
        self._natural_active = identity
        while len(self._natural_offers) > 16:
            removed = self._natural_offers.pop(next(iter(self._natural_offers)))
            self._natural_pruned_unknown_until = max(
                self._natural_pruned_unknown_until, removed.unknown_until
            )
        return cue

    def _resume_natural(self, expected: PresentationCue | None) -> None:
        """timer早醒仍重新核原精确时窗；失效或换句回调没有借下一句执行的机会。"""
        if self._closed or expected is None or self._natural_pending is not expected:
            return
        if not self._natural_scope_current(expected):
            self._clear_natural_pending()
            return
        self._emit(self._base)

    def _acknowledge_natural(
        self, offer: _NaturalOffer, status: str, stroke_started: bool | None
    ) -> bool:
        """仅canceled的明确未发力事实可退自身未知；缺字段、矛盾started和重复终态都不退款。"""
        if status not in {"started", "completed", "canceled", "unavailable"}:
            return False
        if offer.terminal:
            return offer.terminal == status
        if status == "completed" and offer.started_at is None:
            return False
        if status == "started":
            if offer.started_at is not None:
                return True
            now = monotonic()
            offer.started_at = now
            self._natural_starts = [
                start for start in self._natural_starts if now - start[1] < 10.0
            ][-15:]
            self._natural_starts.append((offer.cue.intent.gesture, now))
        else:
            offer.terminal = status
            if offer.started_at is None:
                if status == "canceled" and stroke_started is False:
                    offer.unknown_until = 0.0
                else:
                    offer.unknown_until = max(offer.unknown_until, monotonic() + 10.0)
                self._refresh_natural_unknown()
            if self._natural_active == (offer.cue.turn_id, offer.cue.gesture_id):
                self._natural_active = None
        self._emit(self._base)
        return True

    def _request_user(self, *, complete: bool) -> None:
        """完整查询可早于提交，但同身份在途及含None终态只消费一次，不能续期或重发。"""
        if self._closed:
            return
        if not complete and not self._listening_permitted():
            return
        if self._decider is None:
            if complete and self._user_final:
                self._fail_user()
            return
        identity = (self.turn_id, self._user_text)
        if (
            complete
            and self._user_identity == identity
            and self._user_task_complete
            and self._user_task is not None
        ):
            if self._user_final and self._user_result_ready:
                if self._user_result is None:
                    self._fail_user()
                else:
                    self._adopt_user(self._user_result)
            return
        if (
            not complete
            and self._context.phase == "listening"
            and self._user_identity is not None
            and self._user_identity[0] == self.turn_id
            and self._user_text.startswith(self._user_identity[1])
            and self._user_task is not None
            and not self._user_task.done()
        ):
            # 增量只追加时，前缀始终稳定；不能每个新增词都把600ms计时归零。
            return
        if self._user_identity == identity and self._user_task is not None:
            if not self._user_task.done() and (not complete or self._user_task_complete):
                return
        if self._user_task is not None:
            self._user_task.cancel()
        self._user_identity = identity
        self._user_started = False
        self._user_task_complete = complete
        self._user_result = None
        self._user_result_ready = False
        self._user_revision += 1
        if complete and self._user_final:
            self._prohibited = "all"
            self._feedback = "none"
            self._execution_state = "pending"
            self._emit(self._base)
        context = PresentationContext(
            "thinking" if complete else self._context.phase,
            self.turn_id,
            self._context.revision,
            text=self._user_text,
            user_text=self._user_text,
            last_command=self._last_command,
        )
        self._user_task = asyncio.create_task(
            self._judge_user(context, complete, self._user_revision),
            name="xiaoya-user-presentation",
        )
        self._owned_tasks.add(self._user_task)
        self._user_task.add_done_callback(self._owned_tasks.discard)

    def _matches_user_request(
        self, context: PresentationContext, complete: bool, revision: int
    ) -> bool:
        """成功和失败共用身份边界，连相同全文的倾听升级也不能让旧任务写回状态。"""
        return (
            not self._closed
            and revision == self._user_revision
            and context.turn_id == self.turn_id
            and (
                context.user_text == self._user_text
                or (
                    not complete
                    and not self._user_final
                    and self._context.phase == "listening"
                    and self._user_text.startswith(context.user_text)
                )
            )
        )

    def _listening_permitted(self) -> bool:
        """排程和600ms后共用同一许可，最终转写只能保留神态，不能新发无效轻回应请求。"""
        return (
            not self._closed
            and self._context.phase == "listening"
            and self._user_speaking
            and not self._transcript_final
            and not self._user_final
        )

    def _fail_user(self) -> None:
        """未知用户禁令仍关闭动作许可，失败必须可见，不能等待不存在的指令回执才提示。"""
        self._end_command("canceled", emit=False)
        self._prohibited = "all"
        self._execution_state = "unavailable"
        self._feedback = "retry"
        self._emit(self._base)

    async def _judge_user(
        self, context: PresentationContext, complete: bool, revision: int
    ) -> None:
        """增量守满600ms；最终预取从首次查询计时且只存终态，提交前绝不采纳神态或动作。"""
        try:
            if not complete:
                loop = asyncio.get_running_loop()
                stable_at = loop.time() + 0.6
                while (remaining := stable_at - loop.time()) > 0:
                    await asyncio.sleep(remaining)
                if not self._listening_permitted() or not self._matches_user_request(
                    context, complete, revision
                ):
                    return
            self._user_started = True
            budget = (
                PRESENTATION_TIMING.user_total if complete else PRESENTATION_TIMING.listening_total
            )
            async with asyncio.timeout(budget + PRESENTATION_TIMING.application_grace):
                decision = await self._decider.decide_user(context)
            if not self._matches_user_request(context, complete, revision):
                return
            if complete:
                self._user_result = decision
                self._user_result_ready = True
                if not self._user_final:
                    return
            if decision is None:
                if complete:
                    self._fail_user()
                return
            self._adopt_user(decision)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning("用户表现判断降级（%s）", type(error).__name__)
            if complete and self._matches_user_request(context, complete, revision):
                self._user_result = None
                self._user_result_ready = True
                if self._user_final:
                    self._fail_user()

    def _adopt_user(self, decision: UserPresentationDecision) -> None:
        """回复语义空窗只借用户表情，保留原动作身份和能量；指令下发始终不冒称开始或完成。"""
        if self._user_final and self._execution_state == "pending":
            self._execution_state = "none"
        self._prohibited = choose_user_prohibition(decision)
        if self._prohibited == "all" or (
            self._command is not None and self._command[1] == self._prohibited
        ):
            self._end_command("canceled", emit=False)
        if (
            self._user_final
            and not self._command_turn
            and decision.command != "none"
            and decision.command_confidence >= 0.85
            and decision.command_probability >= 0.90
        ):
            gesture = choose_user_command(decision, self._last_command)
            if gesture == "none" and decision.command == "repeat" and self._last_command == "none":
                self._feedback = "clarify"
                self._execution_state = "clarify"
                self._command_turn = True
            elif gesture != "none" and self._prohibited not in {"all", gesture}:
                self._command_turn = True
                self._feedback = "none"
                self._execution_state = "pendingCommand"
                self._command = (
                    f"{self.turn_id}:command:{uuid4().hex}",
                    gesture,
                    decision.presentation.energy,
                    "pending",
                )
                self._watch_command(1.5)
        if self._context.phase == "listening":
            self._emit(self._current_cue(self._context, decision.presentation))
        elif (
            self._context.phase != "idle"
            and not self._expression_finished
            and not self._reply_expression_adopted
            and decision.presentation.style_confidence >= 0.65
        ):
            self._last_expression = decision.presentation.intent.style
            self._emit(
                replace(
                    self._base,
                    intent=replace(self._base.intent, style=self._last_expression),
                )
            )
        else:
            self._emit(self._base)

    def acknowledge(
        self,
        turn_id: str,
        gesture_id: str,
        status: str,
        stroke_started: bool | None = None,
    ) -> bool:
        """同一v3回执可附真实stroke事实；非法布尔及旧轮拒收，字段缺失保持原未知预算。"""
        if (
            self._closed
            or turn_id != self.turn_id
            or (stroke_started is not None and type(stroke_started) is not bool)
            or (stroke_started is False and status in {"started", "completed"})
        ):
            return False
        natural = self._natural_offers.get((turn_id, gesture_id))
        if natural is not None:
            accepted = self._acknowledge_natural(natural, status, stroke_started)
            _LOGGER.info(
                "自然动作回执",
                extra={
                    "turn_id": turn_id,
                    "gesture_id": gesture_id,
                    "status": status,
                    "accepted": accepted,
                    "monotonic": monotonic(),
                },
            )
            return accepted
        if gesture_id in self._receipts:
            return self._receipts[gesture_id] == status
        command = self._command
        if command is None:
            return False
        if command[0] != gesture_id:
            return False
        if status == "started":
            if command[3] == "started":
                return True
            self._command = (*command[:3], "started")
            self._execution_state = "started"
            self._watch_command(4.0)
            return True
        if status not in {"completed", "canceled", "unavailable"}:
            return False
        if status == "completed" and command[3] != "started":
            return False
        if status == "completed":
            self._last_command = command[1]
        self._end_command(status)
        return True

    def _watch_command(self, delay: float) -> None:
        """每个指令只拥有一个截止任务，既有声音和网络流程不等待客户端确认。"""
        if self._command_watch is not None:
            self._command_watch.cancel()
        self._command_watch = asyncio.create_task(
            self._expire_command(delay, self._command[0]), name="xiaoya-command-deadline"
        )
        self._owned_tasks.add(self._command_watch)
        self._command_watch.add_done_callback(self._owned_tasks.discard)

    async def _expire_command(self, delay: float, identity: str) -> None:
        """丢失回执保留未知结果，不自动补演；更晚的旧成功回执也不写入最近完成记录。"""
        await asyncio.sleep(delay)
        if not self._closed and self._command is not None and self._command[0] == identity:
            self._end_command("unknown")

    def _end_command(self, status: str, *, emit: bool = True) -> None:
        """撤销执行身份后保留本次真实终态，丢回执的 unknown 不能冒充 completed。"""
        command, self._command = self._command, None
        watch, self._command_watch = self._command_watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if command is None:
            return
        self._execution_state = status
        self._receipts[command[0]] = status
        if len(self._receipts) > 16:
            del self._receipts[next(iter(self._receipts))]
        if status in {"unknown", "unavailable"}:
            self._feedback = "retry"
        if emit and not self._closed:
            self._emit(self._base)

    def _request(self, final: bool, context: PresentationContext | None = None) -> None:
        """入口统一保护同一真实语义的在途和失败终态，重复回调不覆盖下一槽或构成隐式重试。"""
        if self._closed or self._decider is None:
            return
        pending = context or self._context
        inflight = self._inflight
        if (
            self._same_segment(self._failed_current, pending)
            or pending == self._failed_preview
            or pending == inflight
            or self._same_segment(inflight, pending)
            or (self._same_segment(self._context, pending) and self._preview_matches(inflight))
        ):
            return
        self._pending = (pending, final)
        if (
            self._decision_task is not None
            and inflight is not None
            and (
                pending.turn_id != inflight.turn_id
                or (
                    pending.phase == "speaking"
                    and inflight != pending
                    and not self._preview_matches(inflight)
                )
                or (
                    pending.reply_preview
                    and not inflight.reply_preview
                    and inflight.phase != "speaking"
                )
            )
        ):
            self._decision_task.cancel()
        self._wake.set()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="xiaoya-presentation")

    async def _run(self) -> None:
        """失败保留原两槽终态；关闭后连吞取消的迟回也直接退出，不能重新等待唤醒。"""
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
            self._decision_task = asyncio.create_task(
                self._decider.decide(context), name="xiaoya-presentation-decision"
            )
            try:
                budget = (
                    PRESENTATION_TIMING.listening_total
                    if context.phase == "listening"
                    else PRESENTATION_TIMING.reply_total
                )
                async with asyncio.timeout(budget + PRESENTATION_TIMING.application_grace):
                    decision = await self._decision_task
            except asyncio.CancelledError:
                if self._closed:
                    raise
                _LOGGER.info("表现判断取消：阶段 %s，正文 %d 字", context.phase, len(context.text))
                continue
            except Exception as error:
                _LOGGER.warning("表现增强已降级（%s）", type(error).__name__)
                self._remember_failure(context)
                self._queue_next_preview(context)
                continue
            finally:
                self._inflight = None
                self._decision_task = None
            if self._closed:
                return
            if decision is None:
                self._remember_failure(context)
                self._queue_next_preview(context)
                continue
            if (
                context == self._context
                or self._same_segment(context, self._context)
                or self._preview_matches(context)
            ):
                self._accepted = (self._context, decision)
                self._emit(self._current_cue(self._context, decision))
                _LOGGER.info("表现判断采纳到当前阶段 %s", self._context.phase)
            elif (
                context == self._preview
                and context.turn_id == self.turn_id
                and context.user_text == self._user_text
            ):
                # 仅缓存即将播放的正文；当前动作、表情和播放身份均不受未来段判断影响。
                self._accepted = (context, decision)
                _LOGGER.info("表现判断缓存到待播正文")
            else:
                _LOGGER.info(
                    "表现判断已过期：原阶段 %s，正文 %d 字，当前阶段 %s，正文 %d 字",
                    context.phase,
                    len(context.text),
                    self._context.phase,
                    len(self._context.text),
                )
            self._queue_next_preview(context)

    def _remember_failure(self, context: PresentationContext) -> None:
        """只记录仍属于当前真实句或待播头槽的失败，过期上下文不能污染下一句或新轮次。"""
        if self._closed:
            return
        if self._context.phase == "speaking" and (
            self._same_segment(context, self._context)
            or context == self._playing_preview
            and self._preview_matches(context)
        ):
            self._failed_current = self._context
        elif (
            context == self._preview
            and context.turn_id == self.turn_id
            and context.user_text == self._user_text
        ):
            self._failed_preview = context

    def _queue_next_preview(self, finished: PresentationContext) -> None:
        """只接续现有下一槽，不能因当前成功或失败补演旧句或再次请求已失败预判。"""
        preview = self._preview
        if (
            preview is not None
            and preview != finished
            and preview != self._inflight
            and preview != self._failed_preview
            and self._pending is None
            and not (self._accepted is not None and self._accepted[0] == preview)
        ):
            self._request(True, preview)

    async def aclose(self) -> None:
        """关闭先撤唯一自然timer和目录，再等待查询资源，旧回执或回调均不可复活。"""
        if self._closed:
            return
        self._closed = True
        self._clear_natural_pending()
        self._natural_active = None
        self._natural_offers.clear()
        self._natural_starts.clear()
        self._natural_unknown_until = 0.0
        self._natural_pruned_unknown_until = 0.0
        self._user_speaking = False
        self._transcript_final = False
        self._user_revision += 1
        self._user_identity = None
        self._user_task_complete = False
        self._user_result = None
        self._user_result_ready = False
        self._user_final = False
        owned = tuple(self._owned_tasks)
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
            self._owned_tasks.difference_update(owned)
        self._end_command("canceled", emit=False)
        self._last_expression = None
        self._expression_finished = True
        self._context = PresentationContext("idle", self.turn_id, self._context.revision + 1)
        self._cue = baseline_cue(self._context)
        self._accepted = None
        self._preview = self._next_preview = None
        self._playing_preview = self._failed_preview = self._failed_current = None
        self._output = None
        self._pending = None
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        if self._decider is not None:
            await self._decider.aclose()
