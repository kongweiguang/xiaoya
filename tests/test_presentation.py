"""验证真实调度的竞争、取消和资源边界，而非把模型期望当作业务事实。"""

import asyncio

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationDecision,
    choose_presentation,
)


class ControlledDecider:
    """事件驱动的异步端口让测试准确选择旧结果迟到的时刻。"""

    def __init__(self) -> None:
        """每个端口独立持有观察和释放事件，避免计时偶然性掩盖跨会话泄漏。"""
        self.contexts = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = 0
        self.cancelled = False

    async def decide(self, context):
        """只在测试释放后返回高分结果，取消会显式记录而不转成成功。"""
        self.contexts.append(context)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return PresentationDecision(DeliveryIntent("curious", "tilt"), 0.99, 0.99, 0.99, 0.7)

    async def aclose(self):
        """计数确保并发关闭仍仅回收本端口一次。"""
        self.closed += 1


def test_jev_is_only_semantic_source_and_does_not_rewrite_committed_voice():
    """声音是已发生事实；视觉只由 Jev 决定，不依赖正文标记或声音预设族。"""
    context = PresentationContext(
        "speaking",
        "turn",
        1,
        reply_id="r",
        segment_id="s",
        voice_style="gentle",
    )
    decision = PresentationDecision(DeliveryIntent("happy", "wave"), 0.99, 0.99, 0.99, 0.8)
    cue = choose_presentation(context, decision)
    assert cue.intent == DeliveryIntent("happy", "wave")
    assert cue.gesture_id == "r:s:wave"
    assert context.voice_style == "gentle"


@pytest.mark.parametrize("phase", ["listening", "thinking"])
def test_non_speaking_never_executes_gesture(phase):
    """倾听和思考可调整表情，却不能因高分动作分类而偷取说话播放许可。"""
    context = PresentationContext(phase, "turn", 1)
    cue = choose_presentation(
        context, PresentationDecision(DeliveryIntent("concerned", "wave"), 1, 1, 1, 0.2)
    )
    assert cue.intent == DeliveryIntent("concerned", "none")
    assert cue.gesture_id == ""


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_probability_cannot_enter_animation(score):
    """运行时数值校验拒绝 JSON 和 Python 的特殊值，避免动画参数永久污染。"""
    with pytest.raises(ValueError):
        PresentationDecision(DeliveryIntent(), score, 0.9, 0.9, 0.5)


async def test_interruption_discards_inflight_result_and_keeps_new_turn():
    """旧 HTTP 即使成功也不能在用户再次开口后把角色切回上一段说话。"""
    port = ControlledDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("想聊聊")
        controller.speak("reply", "segment", DeliveryIntent(), "你想聊什么？")
        await asyncio.wait_for(port.started.wait(), 0.3)
        old_turn = controller.turn_id
        controller.phase("listening")
        assert controller.turn_id != old_turn
        port.release.set()
        await asyncio.sleep(0.01)
        assert cues[-1].phase == "listening"
        assert cues[-1].intent.gesture == "none"
    finally:
        await controller.aclose()


async def test_latest_slot_and_final_revision_replace_pending_requests():
    """处理中的请求之后只消费最新最终文本，不能逐项追赶已经无效的转写历史。"""
    port = ControlledDecider()
    controller = PresentationController(port)
    try:
        controller.complete_turn("第一条")
        await asyncio.wait_for(port.started.wait(), 0.3)
        for text in ("第二条", "第三条", "最后一条"):
            controller.transcribe(text, final=True)
        port.release.set()
        await asyncio.sleep(0.02)
        assert [context.text for context in port.contexts] == ["第一条", "最后一条"]
    finally:
        await controller.aclose()


async def test_close_cancels_request_and_isolates_another_room():
    """一个 Job 的关闭必须取消自己的请求且仅关闭自己的连接池。"""
    first, second = ControlledDecider(), ControlledDecider()
    a, b = PresentationController(first), PresentationController(second)
    a.complete_turn("第一房间")
    b.complete_turn("第二房间")
    await asyncio.gather(first.started.wait(), second.started.wait())
    await asyncio.gather(a.aclose(), a.aclose())
    assert first.closed == 1 and first.cancelled
    assert second.closed == 0 and not second.cancelled
    await b.aclose()


def test_no_key_never_creates_semantic_gesture():
    """没有外部判断时维持自然表现，绑定参数不能成为第二套语义动作来源。"""
    controller = PresentationController()
    controller.complete_turn("挥挥手")
    first = controller.speak("r", "s", DeliveryIntent("happy", "wave"), "你好呀。")
    second = controller.speak("r", "s", DeliveryIntent("happy", "wave"), "你好呀。")
    assert first.gesture_id == second.gesture_id == ""
    assert first.intent.gesture == second.intent.gesture == "none"
    assert first.turn_id == second.turn_id


async def test_reply_preview_runs_before_voice_but_gesture_requires_playback():
    """提前决策可供合成采样，完全相同正文实际播放后才放行动作。"""
    port = ControlledDecider()
    port.release.set()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("你想了解什么？")
        controller.prepare_reply("你最近在做什么呀？")
        await asyncio.wait_for(port.started.wait(), 0.3)
        await asyncio.sleep(0.01)
        assert cues[-1].intent.style == "curious"
        assert cues[-1].phase == "thinking" and cues[-1].gesture_id == ""
        first = controller.speak("r", "s", DeliveryIntent(), "你最近在做什么呀？")
        assert first.intent == DeliveryIntent("curious", "tilt")
        assert first.gesture_id == "r:s:tilt"
    finally:
        await controller.aclose()


class SequencedDecider:
    """每次判断独立释放，让测试按实际竞争边界推进而不依赖计时睡眠。"""

    def __init__(self) -> None:
        """调用和完成分别观察，防止测试在结果已返回却尚未采纳时误判状态。"""
        self.contexts = []
        self.calls = asyncio.Queue()
        self.finished = asyncio.Queue()
        self.closed = 0
        self.cancelled = []

    async def decide(self, context):
        """每个请求等待自己的 Future，取消只记录所属上下文并继续向上传播。"""
        future = asyncio.get_running_loop().create_future()
        self.contexts.append(context)
        self.calls.put_nowait((context, future))
        try:
            return await future
        except asyncio.CancelledError:
            self.cancelled.append(context)
            raise
        finally:
            self.finished.put_nowait(context)

    async def aclose(self):
        """保留关闭次数，证明关闭路径没有替其他房间回收资源。"""
        self.closed += 1

    async def next_call(self):
        """有界等待只作为挂起保护，触发条件来自事件而非设备性能假设。"""
        return await asyncio.wait_for(self.calls.get(), 0.5)

    async def resolve(self, future, decision=None):
        """等待当前 worker 走完同步采纳区间，再让测试读取最终可见状态。"""
        future.set_result(
            decision
            or PresentationDecision(DeliveryIntent("curious", "tilt"), 0.99, 0.99, 0.99, 0.7)
        )
        return await asyncio.wait_for(self.finished.get(), 0.5)


async def test_inflight_preview_promotes_to_current_playback_without_duplicate_request():
    """准确正文的判断正在返回时，播放可继续；迟到预判只升级当前实际片段。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("你在想什么？")
        controller.prepare_reply("想听听你的故事。")
        preview, future = await port.next_call()
        assert preview.reply_preview and preview.phase == "thinking"
        initial = controller.speak("reply", "segment", DeliveryIntent(), preview.text)
        assert initial.intent.gesture == "none"
        assert initial.gesture_id == ""
        await port.resolve(future)
        assert cues[-1].phase == "speaking"
        assert cues[-1].turn_id == preview.turn_id
        assert (cues[-1].reply_id, cues[-1].segment_id) == ("reply", "segment")
        assert cues[-1].gesture_id == "reply:segment:tilt"
        assert len(port.contexts) == 1 and port.calls.empty()
    finally:
        await controller.aclose()


async def test_inflight_preview_cannot_promote_to_next_segment():
    """下一段取得播放许可后，前一段预判成功也不能修改下一段的表情或动作。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("聊聊今天吧。")
        controller.prepare_reply("今天挺开心的。")
        preview, future = await port.next_call()
        controller.speak("reply", "first", DeliveryIntent(), preview.text)
        second = controller.speak("reply", "second", DeliveryIntent(), "明天有什么计划？")
        count_before = len(cues)
        await port.resolve(future)
        context, current_future = await port.next_call()
        assert len(cues) == count_before
        assert context.segment_id == "second" and context.text == "明天有什么计划？"
        assert second.gesture_id == ""
        await port.resolve(current_future)
        assert cues[-1].gesture_id == "reply:second:tilt"
    finally:
        await controller.aclose()


async def test_final_transcript_preserves_reply_preview_and_invalidates_old_semantics():
    """识别把正向动作改为否定时，助手正文仍保留，旧用户语义的预判必须失效。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("挥挥手。")
        controller.prepare_reply("好的。")
        old, old_future = await port.next_call()
        controller.transcribe("不要挥手。", final=True)
        cue = controller.speak("reply", "segment", DeliveryIntent(), "好的。")
        assert cue.intent.gesture == "none"
        count_before = len(cues)
        await port.resolve(old_future)
        fresh, fresh_future = await port.next_call()
        assert fresh.user_text == "不要挥手。" and fresh.text == "好的。"
        assert fresh.turn_id == old.turn_id
        assert len(cues) == count_before
        await port.resolve(
            fresh_future,
            PresentationDecision(DeliveryIntent("gentle", "none"), 0.99, 0.99, 0, 0.2),
        )
        assert cues[-1].intent == DeliveryIntent("gentle", "none")
        assert cues[-1].gesture_id == ""
    finally:
        await controller.aclose()


async def test_transcript_during_preview_keeps_assistant_text_for_next_decision():
    """预判期间的转写修订只更新用户上下文，不能把助手正文替换成识别结果。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("刚才说得不完整。")
        controller.prepare_reply("那你接着说吧。")
        _, old_future = await port.next_call()
        controller.transcribe("刚才说得不完整，我还想继续。", final=True)
        count_before = len(cues)
        await port.resolve(old_future)
        context, fresh_future = await port.next_call()
        assert context.reply_preview and context.text == "那你接着说吧。"
        assert context.user_text == "刚才说得不完整，我还想继续。"
        assert len(cues) == count_before
        await port.resolve(fresh_future)
        assert cues[-1].phase == "thinking" and cues[-1].gesture_id == ""
    finally:
        await controller.aclose()


async def test_new_user_turn_rejects_identical_reply_text_from_old_preview():
    """重复的助手正文不能绕过用户轮次隔离，新轮次必须采用自己的判断。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("第一次请求。")
        controller.prepare_reply("好的。")
        old, old_future = await port.next_call()
        controller.phase("listening")
        controller.complete_turn("第二次请求。")
        controller.prepare_reply("好的。")
        controller.speak("new-reply", "new-segment", DeliveryIntent(), "好的。")
        count_before = len(cues)
        await port.resolve(old_future)
        fresh, fresh_future = await port.next_call()
        assert fresh.turn_id != old.turn_id and fresh.user_text == "第二次请求。"
        assert len(cues) == count_before
        await port.resolve(fresh_future)
        assert cues[-1].gesture_id == "new-reply:new-segment:tilt"
    finally:
        await controller.aclose()


async def test_user_only_decision_cannot_be_reused_as_identical_assistant_preview():
    """用户语义和助手语义即使同字也属于不同输入，不能跳过助手动作判断。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    try:
        controller.complete_turn("你好。")
        context, user_future = await port.next_call()
        assert not context.reply_preview
        await port.resolve(user_future)
        cue = controller.speak("reply", "segment", DeliveryIntent(), "你好。")
        assert cue.intent.gesture == "none"
        reply_context, _ = await port.next_call()
        assert reply_context.phase == "speaking" and reply_context.segment_id == "segment"
    finally:
        await controller.aclose()


async def test_accepted_preview_is_consumed_by_one_segment_even_when_text_repeats():
    """预判晋升后归属首段，下一段同字正文也不能重用已经消费的动作决策。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    try:
        controller.complete_turn("再问一遍好吗？")
        controller.prepare_reply("你确定吗？")
        preview, future = await port.next_call()
        await port.resolve(future)
        first = controller.speak("reply", "first", DeliveryIntent(), preview.text)
        repeat = controller.speak("reply", "first", DeliveryIntent(), preview.text)
        assert first.gesture_id == repeat.gesture_id == "reply:first:tilt"
        second = controller.speak("reply", "second", DeliveryIntent(), preview.text)
        assert second.intent.gesture == "none"
        fresh, _ = await port.next_call()
        assert fresh.segment_id == "second"
    finally:
        await controller.aclose()


async def test_late_inflight_preview_is_consumed_once_after_promotion():
    """播放后才到达的预判也必须记录已消费身份，不能借同字正文进入下一段。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    try:
        controller.complete_turn("再打个招呼。")
        controller.prepare_reply("你好呀。")
        preview, future = await port.next_call()
        controller.speak("reply", "first", DeliveryIntent(), preview.text)
        await port.resolve(future)
        second = controller.speak("reply", "second", DeliveryIntent(), preview.text)
        assert second.gesture_id == "" and second.intent.gesture == "none"
        fresh, _ = await port.next_call()
        assert fresh.segment_id == "second"
    finally:
        await controller.aclose()


async def test_finished_segment_rejects_late_preview_and_cannot_close_next_segment():
    """输出完成是动作许可终点；旧关闭和旧预判都不能影响随后实际播放的片段。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("说两句吧。")
        controller.prepare_reply("第一句。")
        preview, future = await port.next_call()
        controller.speak("reply", "first", DeliveryIntent(), preview.text)
        closed = controller.finish("reply", "first")
        assert closed is not None and closed.phase == "idle"
        count_before = len(cues)
        await port.resolve(future)
        assert len(cues) == count_before
        assert controller.voice_for(preview.text) == DeliveryIntent()
        second = controller.speak("reply", "second", DeliveryIntent(), "第二句。")
        assert controller.finish("reply", "first") is None
        assert second.phase == "speaking" and second.segment_id == "second"
        context, next_future = await port.next_call()
        assert context.segment_id == "second"
        await port.resolve(next_future)
        assert cues[-1].gesture_id == "reply:second:tilt"
    finally:
        await controller.aclose()


async def test_close_revokes_ready_callback_and_every_late_mutation():
    """资源关闭同时撤销准备态与输出授权，迟到的 SDK 回调不能重开表现任务。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    controller.complete_turn("会话准备好了。")
    controller.prepare_reply("那我们开始吧。")
    context, future = await port.next_call()
    await asyncio.gather(controller.aclose(), controller.aclose())
    count_before = len(cues)
    assert future.cancelled() and port.cancelled == [context]
    assert port.closed == 1
    controller.attach(cues.append)
    controller.phase("listening")
    controller.complete_turn("迟到的新输入。")
    controller.transcribe("迟到的识别。", final=True)
    controller.prepare_reply("迟到的回复。")
    cue = controller.speak("late-reply", "late-segment", DeliveryIntent("happy", "wave"), "好。")
    assert cue.phase == "idle" and cue.intent == DeliveryIntent()
    assert controller.finish("late-reply", "late-segment") is None
    assert len(cues) == count_before and len(port.contexts) == 1
    assert controller._worker.done()


@pytest.mark.parametrize(
    ("style_confidence", "gesture_confidence", "perform_probability", "expected"),
    [
        (0.649, 0.99, 0.99, DeliveryIntent("neutral", "wave")),
        (0.65, 0.99, 0.99, DeliveryIntent("happy", "wave")),
        (0.99, 0.849, 0.99, DeliveryIntent("happy", "none")),
        (0.99, 0.85, 0.90, DeliveryIntent("happy", "wave")),
        (0.99, 0.99, 0.899, DeliveryIntent("happy", "none")),
        (0.99, 0.99, 0.90, DeliveryIntent("happy", "wave")),
    ],
)
def test_style_and_gesture_thresholds_have_independent_inclusive_boundaries(
    style_confidence, gesture_confidence, perform_probability, expected
):
    """表情置信度、动作置信度与执行概率各自守门，边界采纳不改变固定声音或强度。"""
    context = PresentationContext(
        "speaking", "turn", 1, reply_id="reply", segment_id="segment", voice_style="neutral"
    )
    cue = choose_presentation(
        context,
        PresentationDecision(
            DeliveryIntent("happy", "wave"),
            style_confidence,
            gesture_confidence,
            perform_probability,
            0.3,
        ),
    )
    assert cue.intent == expected and cue.energy == 0.3
    assert cue.gesture_id == ("reply:segment:wave" if expected.gesture != "none" else "")
    assert context.voice_style == "neutral"


@pytest.mark.parametrize("preview_finishes_first", [True, False])
async def test_future_preview_does_not_change_playing_cue_and_survives_finish(
    preview_finishes_first,
):
    """未来句决策只供合成采样；不论在旧句结束前后到达，都只在下一句播放时执行。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("先说开心的，再说温柔的。")
        controller.speak("reply", "first", DeliveryIntent("happy"), "第一句话。")
        _, current_future = await port.next_call()
        await port.resolve(
            current_future,
            PresentationDecision(DeliveryIntent("happy", "wave"), 0.99, 0.99, 0.99, 0.8),
        )
        current_cue = cues[-1]
        assert current_cue.gesture_id == "reply:first:wave"
        current_context = controller._context
        count_before = len(cues)
        controller.prepare_reply("第二句话。")
        preview, future = await port.next_call()
        assert preview.reply_preview and preview.text == "第二句话。"
        assert controller._context == current_context
        assert controller.voice_for(preview.text) == DeliveryIntent()
        assert cues[-1] == current_cue and len(cues) == count_before
        decision = PresentationDecision(DeliveryIntent("gentle", "nod"), 0.99, 0.99, 0.99, 0.3)
        if preview_finishes_first:
            await port.resolve(future, decision)
            assert controller.voice_for(preview.text) == DeliveryIntent("gentle")
            assert controller._context == current_context
            assert cues[-1] == current_cue and len(cues) == count_before
        closed = controller.finish("reply", "first")
        assert closed.phase == "idle" and closed.intent == DeliveryIntent()
        if not preview_finishes_first:
            await port.resolve(future, decision)
        assert len(cues) == count_before
        assert controller.voice_for(preview.text) == DeliveryIntent("gentle")
        next_cue = controller.speak(
            "reply", "second", controller.voice_for(preview.text), preview.text
        )
        assert next_cue.intent == DeliveryIntent("gentle", "nod")
        assert next_cue.gesture_id == "reply:second:nod" and next_cue.energy == 0.3
        assert next_cue.turn_id == current_cue.turn_id
        assert len(port.contexts) == 2 and port.calls.empty()
    finally:
        await controller.aclose()


async def test_finish_preserves_future_preview_waiting_behind_current_request():
    """唯一请求还在处理当前句时，结束只能撤销当前动作，不能丢弃最新下一句预判槽位。"""
    port = SequencedDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        controller.complete_turn("分两句说吧。")
        controller.speak("reply", "first", DeliveryIntent(), "当前句。")
        _, first_future = await port.next_call()
        controller.prepare_reply("下一句。")
        assert controller.finish("reply", "first").phase == "idle"
        count_before = len(cues)
        await port.resolve(first_future)
        preview, next_future = await port.next_call()
        assert preview.reply_preview and preview.text == "下一句。"
        await port.resolve(next_future)
        assert len(cues) == count_before
        cue = controller.speak("reply", "second", controller.voice_for(preview.text), preview.text)
        assert cue.intent == DeliveryIntent("curious", "tilt")
        assert cue.gesture_id == "reply:second:tilt"
        assert len(port.contexts) == 2 and port.calls.empty()
    finally:
        await controller.aclose()
