"""直接用生产调度器证明首句可提前只读判断，提交、真实播放与撤销仍各守身份。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_jev import Harness, Response

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING
from xiaoya.infrastructure import jev


def answer(style="happy", gesture="wave"):
    """高置信结果暴露提前采纳错误，测试不靠降低原分类或执行门掩盖许可问题。"""
    return PresentationDecision(DeliveryIntent(style, gesture), 1, 1, 1, 0.5)


def port(*, reply=None, command="none"):
    """用户与回复用两个真实端口并行，明确命令也必须等提交后才有执行身份。"""
    user = UserPresentationDecision(
        answer("gentle", "none"),
        command=command,
        command_confidence=1,
        command_probability=float(command != "none"),
    )
    return AsyncMock(
        decide=AsyncMock(return_value=answer(), side_effect=reply),
        decide_user=AsyncMock(return_value=user),
        aclose=AsyncMock(),
    )


async def settled(predicate):
    """仅等待可观察任务状态，半秒只限离线挂起，不能给正式查询增加时间。"""
    async with asyncio.timeout(0.5):
        while not predicate():
            await asyncio.sleep(0)


async def final_input(controller, text="合成问候"):
    """完整ASR早于SDK提交，保留真实生产用户预取任务而不手写控制器内部身份。"""
    controller.user_speaking(True)
    controller.transcribe(text, final=True)
    await controller._user_task


@pytest.mark.asyncio
async def test_first_short_reply_is_readonly_before_commit_and_reused_on_exact_playback():
    """复现首句在listening被丢弃；只存判断，原用户表情和命令也不能提前采纳。"""
    decider = port(command="wave")
    controller = PresentationController(decider)
    emitted = []
    controller.attach(emitted.append)
    try:
        await final_input(controller)
        before, turn = len(emitted), controller.turn_id
        controller.prepare_reply("早上好呀。")
        controller.prepare_reply("早上好呀。")
        await settled(lambda: controller._accepted is not None)
        preview = controller._preview
        assert decider.decide.await_count == 1
        assert preview.text == "早上好呀。" and preview.turn_id == turn
        assert controller._context.phase == "listening" and not controller._context.reply_preview
        assert len(emitted) == before and controller._last_expression is None
        assert controller._cue.intent == DeliveryIntent() and controller._command is None
        assert controller.voice_for(preview.text) == DeliveryIntent()
        controller.user_speaking(False)
        controller.phase("thinking")
        assert controller._preview is preview and controller.turn_id == turn
        controller.complete_turn("合成问候")
        assert controller._preview is preview and controller.turn_id == turn
        assert controller.voice_for(preview.text) == DeliveryIntent("happy")
        assert controller._command is not None and decider.decide_user.await_count == 1
        command = controller._command
        cue = controller.speak("reply", "first", DeliveryIntent(), preview.text)
        assert cue.gesture_source == "command" and controller._command is command
        assert decider.decide.await_count == 1
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_cached_first_preview_supplies_only_its_exact_actual_sentence():
    """提交开放自然动作后，已预判短句无需第二POST，其他句不能借用它的挥手。"""
    decider = port()
    controller = PresentationController(decider)
    try:
        await final_input(controller)
        controller.prepare_reply("早上好呀。")
        await settled(lambda: controller._accepted is not None)
        controller.complete_turn("合成问候")
        cue = controller.speak("reply", "first", DeliveryIntent(), "早上好呀。")
        assert cue.intent == DeliveryIntent("happy", "wave")
        assert cue.gesture_id == "reply:first:wave" and decider.decide.await_count == 1
        cue = controller.speak("reply", "second", DeliveryIntent(), "另一个实际句。")
        assert cue.intent.gesture == "none"
        await settled(lambda: decider.decide.await_count == 2)
        assert decider.decide.call_args.args[0].segment_id == "second"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_reply_preview_does_not_wait_for_user_prefetch_or_adopt_before_commit():
    """最终输入查询尚未返回时首句仍并行预判；提交不等网络，禁令未知前保持原关闭许可。"""
    release = asyncio.Event()
    decider = port()
    user = decider.decide_user.return_value

    async def decide_user(context):
        """完整用户查询刻意挂起，证明首句提前由任务身份授权而不是等待其结果。"""
        await release.wait()
        return user

    decider.decide_user.side_effect = decide_user
    controller = PresentationController(decider)
    try:
        controller.transcribe("合成问候", final=True)
        original = controller._user_task
        controller.prepare_reply("早上好呀。")
        await settled(lambda: controller._accepted is not None)
        assert not original.done() and controller._cue.intent == DeliveryIntent()
        controller.complete_turn("合成问候")
        cue = controller.speak("reply", "first", DeliveryIntent(), "早上好呀。")
        assert controller._user_task is original and not original.done()
        assert cue.intent.style == "happy" and cue.intent.gesture == "none"
        assert controller._prohibited == "all" and decider.decide.await_count == 1
        release.set()
        await original
        assert controller._cue.intent == DeliveryIntent("happy", "wave")
        assert controller._cue.gesture_id == "reply:first:wave"
        assert decider.decide_user.await_count == 1
    finally:
        release.set()
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["none", "exception"])
async def test_failed_precommit_preview_stays_terminal_through_commit(outcome):
    """失败绑定原头槽并晋升实际片段，不能把提交或重复final当自动重试。"""
    decider = port(reply=ValueError("合成失败") if outcome == "exception" else None)
    if outcome == "none":
        decider.decide.return_value = None
    controller = PresentationController(decider)
    try:
        await final_input(controller)
        controller.prepare_reply("早上好呀。")
        await settled(lambda: controller._failed_preview is not None)
        controller.phase("thinking")
        controller.complete_turn("合成问候")
        controller.transcribe("合成问候", final=True)
        first = controller.speak("reply", "first", DeliveryIntent(), "早上好呀。")
        controller.speak("reply", "first", DeliveryIntent(), "早上好呀。")
        await settled(lambda: controller._pending is None and controller._inflight is None)
        assert first.intent.gesture == "none" and decider.decide.await_count == 1
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_precommit_inflight_keeps_real_adapter_deadline_and_single_post(monkeypatch):
    """保留真实HTTP适配器的截止与任务，不以重发或延长3.5秒补首句预判空档。"""
    http = Harness()
    monkeypatch.setattr(jev.aiohttp, "ClientSession", http.client)
    release = asyncio.Event()
    held = Response(wait=release)
    http.responses.append(held)
    controller = PresentationController(http.decider())
    try:
        await final_input(controller)
        controller.prepare_reply("早上好呀。")
        await settled(lambda: held.content.started.is_set())
        task, preview = controller._decision_task, controller._preview
        timeout = http.calls[0][1]["timeout"].total
        assert 0 < timeout <= PRESENTATION_TIMING.reply_total == 3.5
        controller.user_speaking(False)
        controller.phase("thinking")
        controller.complete_turn("合成问候")
        controller.speak("reply", "first", DeliveryIntent(), "早上好呀。")
        assert controller._decision_task is task and not task.cancelling()
        assert controller._playing_preview is preview and len(http.calls) == 1
        release.set()
        await settled(lambda: controller._cue.intent.gesture == "wave")
        assert len(http.calls) == 1 and http.calls[0][1]["timeout"].total == timeout
        assert held.exits == 1
    finally:
        release.set()
        await controller.aclose()


@pytest.mark.asyncio
async def test_precommit_keeps_two_slots_and_never_borrows_next_sentence(monkeypatch):
    """两槽身份和预判保持独立；下一句真实播放仍须等上一实际started后的原3秒节奏。"""
    clock = [100.0]
    monkeypatch.setattr("xiaoya.application.presentation.monotonic", lambda: clock[0])
    decider = port(reply=[answer(), answer("curious", "tilt")])
    controller = PresentationController(decider)
    try:
        await final_input(controller)
        controller.prepare_reply("第一短句。")
        controller.prepare_reply("第二短句。")
        controller.prepare_reply("第三短句。")
        await settled(lambda: controller._accepted is not None)
        assert decider.decide.await_count == 1
        assert controller._preview.text == "第一短句。"
        assert controller._next_preview.text == "第二短句。"
        controller.complete_turn("合成问候")
        first = controller.speak("reply", "first", DeliveryIntent(), "第一短句。")
        assert first.intent.gesture == "wave"
        assert controller.acknowledge(first.turn_id, first.gesture_id, "started")
        await settled(lambda: decider.decide.await_count == 2 and controller._inflight is None)
        assert controller._cue.segment_id == "first" and controller._cue.intent.gesture == "wave"
        second = controller.speak("reply", "second", DeliveryIntent(), "第二短句。")
        assert second.intent == DeliveryIntent("curious") and second.gesture_id == ""
        assert controller._natural_pending.gesture_id == "reply:second:tilt"
        assert controller._natural_pending.segment_id == second.segment_id == "second"
        assert controller.acknowledge(first.turn_id, first.gesture_id, "completed")
        clock[0] = 103.0
        controller._resume_natural(controller._natural_pending)
        second = controller._cue
        assert second.intent == DeliveryIntent("curious", "tilt")
        assert [call.args[0].text for call in decider.decide.call_args_list] == [
            "第一短句。",
            "第二短句。",
        ]
    finally:
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["same-partial", "changed-final", "empty-final", "idle", "new-turn", "close"]
)
async def test_late_precommit_reply_cannot_revive_even_with_identical_replacement(boundary):
    """旧请求故意吞取消并迟回；同文partial再final也须撤原代次，不能靠正文相同复活。"""
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def decide(context):
        """首任务吞取消返回高分旧动作，后续真实同文片段保持低动作以暴露错误借用。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return answer()
        return answer("curious", "none")

    decider = port(reply=decide)
    controller = PresentationController(decider)
    closing = None
    try:
        await final_input(controller)
        controller.prepare_reply("相同短句。")
        await settled(started.is_set)
        old = controller._preview
        if boundary == "same-partial":
            controller.transcribe("合成问候", final=False)
            controller.transcribe("合成问候", final=True)
            await controller._user_task
        elif boundary == "changed-final":
            controller.transcribe("合成新问候", final=True)
            await controller._user_task
        elif boundary == "empty-final":
            controller.transcribe("", final=True)
        elif boundary == "idle":
            controller.phase("idle")
        elif boundary == "new-turn":
            controller.phase("idle")
            await final_input(controller)
        else:
            closing = asyncio.create_task(controller.aclose())
            await settled(lambda: controller._closed)
        assert controller._preview is None and controller._next_preview is None
        if boundary in {"same-partial", "changed-final", "new-turn"}:
            controller.prepare_reply("相同短句。")
            assert controller._preview != old
            controller.complete_turn(controller._user_text)
            controller.speak("reply", "first", DeliveryIntent(), "相同短句。")
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, 0.5)
            assert controller._worker.done()
        else:
            await settled(lambda: controller._inflight is None and controller._pending is None)
        assert controller._cue.intent.gesture != "wave"
        assert controller._last_expression != "happy"
    finally:
        release.set()
        await controller.aclose()


@pytest.mark.asyncio
async def test_mismatching_commit_discards_readonly_preview_and_keeps_new_input_independent():
    """提交正文与最终ASR不同即无复用许可，不能把旧问候判断借给同轮新输入。"""
    decider = port()
    controller = PresentationController(decider)
    try:
        await final_input(controller)
        controller.prepare_reply("相同短句。")
        await settled(lambda: controller._accepted is not None)
        controller.complete_turn("合成另一次输入")
        assert controller._preview is None and controller._next_preview is None
        assert controller._accepted is None
        cue = controller.speak("reply", "first", DeliveryIntent(), "相同短句。")
        assert cue.intent.gesture == "none"
        await settled(lambda: decider.decide.await_count == 2)
        assert decider.decide.call_args.args[0].user_text == "合成另一次输入"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_uncommitted_tool_playback_cannot_consume_final_input_preview():
    """工具分支进入实际播放也不能替SDK提交授权；旧只读结果撤销，新typed同文独立开轮。"""
    decider = port(reply=[answer(), answer("curious", "none")])
    controller = PresentationController(decider)
    try:
        await final_input(controller)
        controller.prepare_reply("相同短句。")
        await settled(lambda: controller._accepted is not None)
        turn = controller.turn_id
        cue = controller.speak("tool-reply", "first", DeliveryIntent(), "相同短句。")
        assert cue.intent.style != "happy" and cue.intent.gesture == "none"
        assert controller._preview is None and controller._accepted is None
        await settled(lambda: decider.decide.await_count == 2)
        assert decider.decide.call_args.args[0].reply_id == "tool-reply"
        controller.complete_turn("合成问候")
        assert controller.turn_id != turn
        assert controller._accepted is None and controller._preview is None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_partial_does_not_open_precommit_reply_slot_or_bypass_stability():
    """只有完整最终输入可提前判断回复，增量仍由原600ms稳定窗口负责倾听。"""
    decider = port()
    controller = PresentationController(decider)
    try:
        controller.user_speaking(True)
        controller.transcribe("合成未说完", final=False)
        controller.prepare_reply("不应查询。")
        await asyncio.sleep(0)
        assert controller._preview is None and decider.decide.await_count == 0
        assert decider.decide_user.await_count == 0
        controller.transcribe("合成未说完", final=True)
        await controller._user_task
        assert decider.decide_user.await_count == 1
    finally:
        await controller.aclose()
