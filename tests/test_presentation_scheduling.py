"""期望行为的红绿回归：所有时间、语义门与网络端口继续使用正式实现。"""

import asyncio
import hashlib
import json

import pytest
import test_jev as HARNESS

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.infrastructure import jev


@pytest.fixture
def controller_type():
    """正式测试直接构造生产类，隔离运行器只在独立进程替换该模块，不引入产品替身。"""
    return PresentationController


async def settled(predicate):
    """等待确定状态且仅给离线挂起保护，不能修改产品预算或以睡眠假装网络成功。"""
    async with asyncio.timeout(0.5):
        while not predicate():
            await asyncio.sleep(0)


def body_hash(call):
    """合成payload只比较规范SHA，证明相同正文不重发且新轮次仍可独立请求。"""
    return hashlib.sha256(
        json.dumps(call[1]["json"], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@pytest.fixture
def http(monkeypatch):
    """只替换外部HTTP端口，实际应用调度、端口缓存、解析和时间常量均不替换。"""
    harness = HARNESS.Harness()
    monkeypatch.setattr(jev.aiohttp, "ClientSession", harness.client)
    return harness


@pytest.mark.asyncio
async def test_failed_preview_is_not_retried_on_same_playback(http, controller_type):
    """失败预判晋升只回退；同一真实片段重复绑定也不能把失败当成尚未请求。"""
    http.responses.extend([HARNESS.Response(error=TimeoutError()), HARNESS.Response()])
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成问候")
        controller.prepare_reply("合成答句。")
        await settled(lambda: len(http.calls) == 1 and controller._inflight is None)
        first = controller.speak("reply", "first", DeliveryIntent(), "合成答句。")
        controller.speak("reply", "first", DeliveryIntent(), "合成答句。")
        await settled(lambda: controller._pending is None and controller._inflight is None)
        assert len(http.calls) == 1
        assert first.intent.gesture == "none"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_same_text_new_segment_and_new_turn_are_not_globally_deduplicated(
    http,
    controller_type,
):
    """失败只能属于已消费的原槽；同字新segment和新用户轮次必须获得独立判断。"""
    http.responses.extend(
        [
            HARNESS.Response(error=TimeoutError()),
            HARNESS.Response(),
            HARNESS.Response(),
        ]
    )
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成问候")
        controller.prepare_reply("同字答句。")
        await settled(lambda: len(http.calls) == 1 and controller._inflight is None)
        controller.speak("reply", "first", DeliveryIntent(), "同字答句。")
        controller.finish("reply", "first")
        controller.prepare_reply("同字答句。")
        await settled(lambda: len(http.calls) >= 2 and controller._inflight is None)
        controller.speak("reply", "second", DeliveryIntent(), "同字答句。")
        assert len(http.calls) == 2
        controller.complete_turn("合成新轮次")
        controller.prepare_reply("同字答句。")
        await settled(lambda: len(http.calls) >= 3 and controller._inflight is None)
        assert len(http.calls) == 3
        assert body_hash(http.calls[0]) != body_hash(http.calls[2])
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_repeated_actual_playback_keeps_own_inflight_post(http, controller_type):
    """实际片段的修订变化不是新语义；重复SDK更新不能取消并重发同一请求或抢走下一槽。"""
    response = HARNESS.Response(wait=asyncio.Event())
    http.responses.extend([response, HARNESS.Response()])
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成同段更新")
        controller.speak("reply", "first", DeliveryIntent(), "合成当前句。")
        await response.content.started.wait()
        first_task = controller._decision_task
        controller.prepare_reply("合成下一句。")
        controller.speak("reply", "first", DeliveryIntent(), "合成当前句。")
        await asyncio.sleep(0)
        assert controller._decision_task is first_task and not first_task.cancelling()
        assert len(http.calls) == 1 and response.exits == 0
        assert controller._pending[0].text == "合成下一句。"
        response.content.wait.set()
        await settled(lambda: len(http.calls) == 2 and controller._inflight is None)
        assert http.calls[1][1]["json"]["state"]["assistant"] == "合成下一句。"
        assert controller._cue.intent.gesture == "wave"
        assert controller._cue.reply_id == "reply" and controller._cue.segment_id == "first"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_repeated_final_transcript_cannot_retry_failed_current(http, controller_type):
    """相同final回调也经过中央失败门；只有用户正文真正变更才有新的语义判断。"""
    http.responses.extend(
        [
            HARNESS.Response(error=TimeoutError()),
            HARNESS.Response(),
            HARNESS.Response(),
        ]
    )
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成用户原文")
        controller.speak("reply", "first", DeliveryIntent(), "合成当前句。")
        await settled(lambda: len(http.calls) == 1 and controller._inflight is None)
        controller.transcribe("合成用户原文", final=True)
        await settled(lambda: controller._pending is None and controller._inflight is None)
        assert len(http.calls) == 1
        controller.transcribe("合成用户补充内容", final=True)
        await settled(lambda: len(http.calls) == 2 and controller._inflight is None)
        assert http.calls[1][1]["json"]["state"]["user"] == "合成用户补充内容"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "inflight", "accepted"])
async def test_same_final_preserves_preview_semantic_identity(http, controller_type, outcome):
    """重复final不重建尚未播放的槽；失败、在途和已采纳都不得因纯修订再付费。"""
    response = HARNESS.Response(
        error=TimeoutError() if outcome == "failed" else None,
        wait=asyncio.Event() if outcome == "inflight" else None,
    )
    http.responses.extend([response, HARNESS.Response()])
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成用户原文")
        controller.prepare_reply("合成待播句。")
        await response.content.started.wait()
        if outcome != "inflight":
            await settled(lambda: controller._inflight is None)
        preview = controller._preview
        controller.transcribe("合成用户原文", final=True)
        if outcome == "inflight":
            response.content.wait.set()
        await settled(lambda: controller._pending is None and controller._inflight is None)
        assert controller._preview is preview
        assert len(http.calls) == 1
        controller.speak("reply", "first", DeliveryIntent(), "合成待播句。")
        await settled(lambda: controller._pending is None and controller._inflight is None)
        assert len(http.calls) == 1
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_same_partial_after_vad_resume_restarts_real_stability_window(
    http,
    controller_type,
    monkeypatch,
):
    """final预取独立于倾听；同文恢复仍从头守满600ms，缓存HTTP不能代替端口时刻证明。"""
    entered = []
    decider = http.decider()
    original_decide = decider.decide_user

    async def observed_decision(context):
        """只记录正式端口被调用的单调时钟，保留真实HTTP契约和原判断结果。"""
        entered.append(asyncio.get_running_loop().time())
        return await original_decide(context)

    monkeypatch.setattr(decider, "decide_user", observed_decision)
    controller = controller_type(decider)
    try:
        controller.user_speaking(True)
        controller.transcribe("合成连续增量", final=False)
        await asyncio.sleep(0.3)
        turn = controller.turn_id
        controller.transcribe("合成连续增量", final=True)
        await controller._user_task
        assert len(entered) == len(http.request_calls) == 1 and controller._command is None
        controller.user_speaking(False)
        controller.user_speaking(True)
        resumed_at = asyncio.get_running_loop().time()
        controller.transcribe("合成连续增量", final=False)
        task = controller._user_task
        assert task is not None
        await asyncio.sleep(0.35)
        assert len(entered) == len(http.request_calls) == 1
        await asyncio.wait_for(task, 0.6)
        assert len(entered) == 2 and entered[1] - resumed_at >= 0.6
        assert controller.turn_id == turn and len(http.request_calls) == 1
        assert controller._command is None and not controller._user_final
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_late_current_failure_does_not_retry_and_advances_existing_next(
    http,
    controller_type,
):
    """预判已晋升后失败仍属于当前句；下一槽应接续，其正文和动作不能借给旧句。"""
    release = asyncio.Event()
    old = HARNESS.Response(wait=release, error=TimeoutError())
    http.responses.extend([old, HARNESS.Response()])
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成两句")
        controller.prepare_reply("第一合成句。")
        await old.content.started.wait()
        controller.speak("reply", "first", DeliveryIntent(), "第一合成句。")
        controller.prepare_reply("第二合成句。")
        release.set()
        await settled(lambda: len(http.calls) >= 2 and controller._inflight is None)
        controller.speak("reply", "first", DeliveryIntent(), "第一合成句。")
        await settled(lambda: controller._pending is None and controller._inflight is None)
        assert len(http.calls) == 2
        assert http.calls[1][1]["json"]["state"]["assistant"] == "第二合成句。"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("preview_origin", [False, True])
async def test_finish_cancels_only_current_and_advances_queued_next(
    http,
    controller_type,
    preview_origin,
):
    """已结束当前无采纳窗口，必须释放；覆盖无预判和预判晋升两种精确映射。"""
    old = HARNESS.Response(wait=asyncio.Event())
    next_response = HARNESS.Response(wait=asyncio.Event())
    http.responses.extend([old, next_response])
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成两句")
        if preview_origin:
            controller.prepare_reply("当前合成句。")
            await old.content.started.wait()
        controller.speak("reply", "first", DeliveryIntent(), "当前合成句。")
        await old.content.started.wait()
        controller.prepare_reply("下一合成句。")
        controller.finish("reply", "first")
        await settled(lambda: old.exits == 1 and len(http.calls) == 2)
        assert not next_response.content.wait.is_set()
        assert controller.finish("reply", "first") is None
        await asyncio.sleep(0)
        assert next_response.exits == 0
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_finish_does_not_cancel_already_running_next_preview(http, controller_type):
    """当前判断成功后运行下一预判时，旧finish不拥有它；重复旧finish也无取消权。"""
    next_response = HARNESS.Response(wait=asyncio.Event())
    http.responses.extend([HARNESS.Response(), next_response])
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成两句")
        controller.speak("reply", "first", DeliveryIntent(), "第一合成句。")
        await settled(lambda: controller._accepted is not None)
        controller.prepare_reply("第二合成句。")
        await next_response.content.started.wait()
        controller.finish("reply", "first")
        await asyncio.sleep(0)
        assert next_response.exits == 0
        controller.speak("reply", "second", DeliveryIntent(), "第二合成句。")
        assert controller.finish("reply", "first") is None
        await asyncio.sleep(0)
        assert next_response.exits == 0
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_idle_releases_ended_current_query_without_new_post(http, controller_type):
    """SDK可能先idle后done，公开结束阶段同样不能让无效当前请求继续占位。"""
    response = HARNESS.Response(wait=asyncio.Event())
    http.responses.append(response)
    controller = controller_type(http.decider())
    try:
        controller.complete_turn("合成输入")
        controller.speak("reply", "first", DeliveryIntent(), "合成答句。")
        await response.content.started.wait()
        controller.phase("idle")
        await settled(lambda: response.exits == 1)
        assert len(http.calls) == 1
    finally:
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["vad", "final", "both"])
async def test_final_prefetch_is_separate_from_ineligible_listening(
    http, controller_type, boundary
):
    """停声增量仍不查询；final只读预取完整预算，但不能借此采纳神态或执行命令。"""
    controller = controller_type(http.decider())
    try:
        controller.user_speaking(True)
        if boundary in {"vad", "both"}:
            controller.user_speaking(False)
        controller.transcribe("合成最终文本", final=boundary != "vad")
        task = controller._user_task
        if task is not None:
            await asyncio.wait_for(task, 1)
        assert len(http.request_calls) == (0 if boundary == "vad" else 1)
        assert controller._command is None and not controller._user_final
        assert controller._last_expression is None and controller._cue.intent == DeliveryIntent()
        if http.request_calls:
            assert http.request_calls[0][1]["timeout"].total == 8
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_stability_wait_rechecks_permission_even_without_cancel_callback(
    http,
    controller_type,
):
    """发送前守卫必须独立成立；仅依赖取消回调无法保护已到期、尚待调度的任务。"""
    controller = controller_type(http.decider())
    try:
        controller.user_speaking(True)
        controller.transcribe("合成稳定增量", final=False)
        # 模拟边界状态已更新而取消回调尚未派发，只验证发送前独立重核，不更改业务时钟。
        controller._transcript_final = True
        await asyncio.wait_for(controller._user_task, 1)
        assert not http.request_calls
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_partial_final_upgrade_keeps_one_post_and_command_is_independent(
    http,
    controller_type,
):
    """已有合法partial可共享同全文完整请求，撤轻回应不能撤正在确认的完整指令。"""
    body = HARNESS.response_body()
    answer = body["answers"]["requested_gesture"]
    answer["choice"] = "wave"
    answer["probabilities"] = {name: float(name == "wave") for name in jev.COMMAND_LABELS}
    response = HARNESS.Response(body=body, wait=asyncio.Event())
    http.request_responses.append(response)
    controller = controller_type(http.decider())
    try:
        controller.user_speaking(True)
        controller.transcribe("合成挥手指令", final=False)
        await asyncio.wait_for(response.content.started.wait(), 1)
        controller.transcribe("合成挥手指令", final=True)
        assert controller._command is None
        controller.complete_turn("合成挥手指令")
        controller.user_speaking(False)
        response.content.wait.set()
        await asyncio.wait_for(controller._user_task, 0.5)
        assert len(http.request_calls) == 1
        assert controller._command is not None and controller._command[1] == "wave"
        identity = controller._command[0]
        controller.phase("idle")
        assert controller._command[0] == identity
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_close_clears_all_failed_slots_and_new_turn_reopens_same_text(http, controller_type):
    """新轮次、关闭回收有界失败槽，不能把旧失败当永续负缓存，也不保留下一句正文。"""
    http.responses.extend([HARNESS.Response(error=TimeoutError()), HARNESS.Response()])
    controller = controller_type(http.decider())
    controller.complete_turn("合成一轮")
    controller.prepare_reply("同字合成句。")
    await settled(lambda: len(http.calls) == 1 and controller._inflight is None)
    controller.complete_turn("合成新一轮")
    controller.prepare_reply("同字合成句。")
    await settled(lambda: len(http.calls) == 2 and controller._inflight is None)
    controller.prepare_reply("下一合成句。")
    await controller.aclose()
    assert controller._preview is None and controller._next_preview is None
    for name in ("_failed_preview", "_failed_current", "_playing_preview"):
        assert getattr(controller, name, None) is None
