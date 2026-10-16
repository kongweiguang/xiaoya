"""最终转写只提前网络查询，真实提交才允许采纳；测试不连接模型或房间。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_jev import Harness, Response, response_body

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING
from xiaoya.infrastructure import jev


@pytest.fixture
def http(monkeypatch):
    """只替换外部HTTP工厂，保留真实适配器的缓存、完整时钟和请求所有权。"""
    harness = Harness()
    monkeypatch.setattr(jev.aiohttp, "ClientSession", harness.client)
    return harness


def evidence(command="wave", style="gentle"):
    """明确可过原阈值的结果用于检查授权时点，不靠降低置信度阻止提前执行。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent(style, "tilt"), 1, 1, 1, 0.5),
        command=command,
        command_confidence=1,
        command_probability=float(command != "none"),
    )


def response(*, wait=None, error=None):
    """合法HTTP证据同时带表情与命令，暴露任何提交前采纳而非只观察POST计数。"""
    body = response_body()
    body["answers"]["requested_gesture"].update(
        choice="wave",
        probabilities={name: float(name == "wave") for name in jev.COMMAND_LABELS},
    )
    return Response(body=body, wait=wait, error=error)


async def flush():
    """只推进已就绪回调，不能用额外时间等待掩盖预算或提交许可的错误。"""
    for _ in range(15):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_final_prefetch_finishes_read_only_then_commit_consumes_result(http):
    """ASR最终文本应立刻查询，但结果不得改变神态、禁令或命令，提交后复用终态。"""
    http.request_responses.append(response())
    decider = http.decider()
    controller = PresentationController(decider)
    try:
        controller.user_speaking(True)
        controller.transcribe("合成挥挥手", final=True)
        task = controller._user_task
        assert task is not None
        await task
        assert len(http.request_calls) == 1
        assert not controller._user_final and controller._context.phase == "listening"
        assert controller._command is None and controller._prohibited == "all"
        assert controller._last_expression is None and controller._cue.intent.style == "neutral"
        assert controller._execution_state == "none" and controller._feedback == "none"
        controller.user_speaking(False)
        controller.complete_turn("合成挥挥手")
        assert controller._user_task is task and len(http.request_calls) == 1
        assert controller._command is not None and controller._command[1] == "wave"
        assert controller._cue.intent.style == "happy"
        command = controller._command
        controller.transcribe("合成挥挥手", final=True)
        await flush()
        assert controller._user_task is task and len(http.request_calls) == 1
        assert controller._command is command
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_final_inflight_survives_vad_and_thinking_without_resetting_eight_seconds(http):
    """SDK提交只接管原任务和原8秒截止；正常停声及先发thinking都不能丢失预取。"""
    release = asyncio.Event()
    held = response(wait=release)
    http.request_responses.append(held)
    decider = http.decider()
    controller = PresentationController(decider)
    try:
        controller.user_speaking(True)
        started = asyncio.get_running_loop().time()
        controller.transcribe("合成挥挥手", final=True)
        task = controller._user_task
        assert task is not None
        await asyncio.wait_for(held.content.started.wait(), 0.5)
        timer = decider._user_timeout
        assert timer is not None
        deadline = timer.when()
        assert deadline <= asyncio.get_running_loop().time() + 8
        assert deadline >= started + 8
        assert http.request_calls[0][1]["timeout"].total == PRESENTATION_TIMING.user_total == 8
        await asyncio.sleep(0.02)
        controller.transcribe("合成挥挥手", final=True)
        controller.user_speaking(False)
        controller.phase("thinking")
        controller.complete_turn("合成挥挥手")
        await flush()
        assert controller._user_task is task and not task.cancelling()
        assert decider._user_timeout is timer and timer.when() == deadline
        assert len(http.request_calls) == 1 and controller._command is None
        release.set()
        await task
        assert controller._command is not None and controller._command[1] == "wave"
    finally:
        release.set()
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "exception"])
async def test_failed_final_prefetch_is_terminal_for_repeated_final_and_commit(failure):
    """连违反端口约定的异常也要成为当前输入终态，不能提交时重发或提前显示失败。"""
    port = AsyncMock(
        decide_user=AsyncMock(
            side_effect=ValueError("合成端口失败") if failure == "exception" else None,
            return_value=None,
        ),
        aclose=AsyncMock(),
    )
    controller = PresentationController(port)
    try:
        controller.transcribe("合成挥手请求", final=True)
        task = controller._user_task
        assert task is not None
        await task
        assert controller._execution_state == "none" and controller._feedback == "none"
        controller.transcribe("合成挥手请求", final=True)
        controller.complete_turn("合成挥手请求")
        await flush()
        assert controller._user_task is task and port.decide_user.await_count == 1
        assert controller._command is None and controller._prohibited == "all"
        assert controller._execution_state == "unavailable" and controller._feedback == "retry"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_failed_http_prefetch_is_not_reposted_and_close_releases_owned_resources(http):
    """实际HTTP失败缓存与应用终态共同阻止重复POST，关闭还需回收原响应和适配器任务。"""
    failed = response(error=TimeoutError())
    http.request_responses.append(failed)
    decider = http.decider()
    original = decider.decide_user
    decider.decide_user = AsyncMock(wraps=original)
    controller = PresentationController(decider)
    controller.transcribe("合成挥手请求", final=True)
    task = controller._user_task
    try:
        assert task is not None
        await task
        controller.transcribe("合成挥手请求", final=True)
        controller.complete_turn("合成挥手请求")
        await flush()
        assert controller._user_task is task
        assert decider.decide_user.await_count == len(http.request_calls) == 1
        assert controller._execution_state == "unavailable"
    finally:
        await controller.aclose()
    assert failed.exits == 1 and not decider._queries and not controller._owned_tasks
    assert decider._client is None and decider._user_timeout is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["changed-final", "partial", "same-partial", "empty-final", "new-turn", "close"]
)
async def test_invalidated_prefetch_cannot_revive_after_late_result(boundary):
    """供应商忽略取消也不能越过改文、继续说话或关闭代次写回表情、禁令和动作。"""
    entered, release = asyncio.Event(), asyncio.Event()

    async def decide(context):
        """旧最终文本故意吞取消；后续合法输入独立返回，直接暴露旧代次采纳漏洞。"""
        if context.user_text == "合成旧请求" and context.phase != "listening":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return evidence("wave", "happy")
        return evidence("none", "curious")

    port = AsyncMock(decide_user=AsyncMock(side_effect=decide), aclose=AsyncMock())
    controller = PresentationController(port)
    closing = None
    try:
        controller.user_speaking(True)
        controller.transcribe("合成旧请求", final=True)
        old = controller._user_task
        assert old is not None
        await asyncio.wait_for(entered.wait(), 0.5)
        if boundary == "changed-final":
            controller.transcribe("合成新请求", final=True)
            controller.complete_turn("合成新请求")
            await controller._user_task
        elif boundary in {"partial", "same-partial"}:
            controller.transcribe(
                "合成旧请求" if boundary == "same-partial" else "合成继续说话", final=False
            )
        elif boundary == "empty-final":
            controller.transcribe("", final=True)
        elif boundary == "new-turn":
            controller.phase("idle")
            controller.user_speaking(True)
        else:
            closing = asyncio.create_task(controller.aclose())
            await flush()
        release.set()
        await old
        if closing is not None:
            await closing
        assert controller._command is None
        assert controller._cue.intent.style != "happy"
        assert controller._prohibited == ("none" if boundary == "changed-final" else "all")
        if boundary in {"partial", "same-partial"}:
            assert not controller._user_final
            assert port.decide_user.await_count == 1
    finally:
        release.set()
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "speaking"])
async def test_abandoned_phase_cannot_reuse_uncommitted_prefetch(http, phase):
    """只允许当前倾听或思考候选晋升；已退待机或进入别的回复后，同文提交仍是新轮。"""
    http.request_responses.extend([response(), response()])
    controller = PresentationController(http.decider())
    try:
        controller.transcribe("合成挥挥手", final=True)
        old_task, old_turn = controller._user_task, controller.turn_id
        await old_task
        if phase == "speaking":
            controller.speak("reply", "segment", DeliveryIntent(), "合成现有答句。")
        else:
            controller.phase(phase)
        controller.complete_turn("合成挥挥手")
        await controller._user_task
        assert controller.turn_id != old_turn and controller._user_task is not old_task
        assert len(http.request_calls) == 2 and controller._command is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_same_text_in_new_turn_is_not_global_prefetch_cache(http):
    """只去重当前轮相同final；用户下一轮确实重复命令时仍应生成新查询与执行身份。"""
    http.request_responses.extend([response(), response()])
    controller = PresentationController(http.decider())
    try:
        controller.transcribe("合成挥挥手", final=True)
        assert controller._user_task is not None
        await controller._user_task
        controller.complete_turn("合成挥挥手")
        first = controller._command
        assert first is not None
        controller.user_speaking(True)
        controller.transcribe("合成挥挥手", final=True)
        await controller._user_task
        controller.complete_turn("合成挥挥手")
        assert len(http.request_calls) == 2
        assert controller._command is not None and controller._command[0] != first[0]
    finally:
        await controller.aclose()
