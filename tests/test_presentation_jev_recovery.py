"""组合真实应用与 Jev 适配器，只替换 HTTP 边界，捕获两层预算和取消所有权的缺口。"""

import asyncio
from types import SimpleNamespace

import pytest
from test_jev import Harness, Response, response_body

from xiaoya.application import presentation
from xiaoya.application.presentation import PresentationController
from xiaoya.infrastructure import jev


@pytest.fixture
def http(monkeypatch):
    """复用供应商合同夹具且封住唯一网络工厂，测试绝不读取真实密钥或连接外部模型。"""
    harness = Harness()
    monkeypatch.setattr(jev.aiohttp, "ClientSession", harness.client)
    monkeypatch.setattr(jev, "time", harness.clock)
    return harness


def command_response(gesture="wave", *, wait=None):
    """构造可通过实际解析器与领域门槛的用户证据，不替换应用的授权或动作生成。"""
    body = response_body()
    body["answers"]["requested_gesture"].update(
        choice=gesture,
        confidence=1.0,
        probabilities={name: float(name == gesture) for name in jev.COMMAND_LABELS},
    )
    return Response(body=body, wait=wait)


def held_head(http):
    """连接阻塞在响应头之前，模拟代理 TLS 的冷启动而不消耗语义响应队列。"""
    response = Response()
    response.enter_wait = asyncio.Event()
    response.enter_started = asyncio.Event()
    http.warm_responses.append(response)
    return response


async def flush():
    """只推进已就绪的采纳和清理，迟到请求必须自己具备继续执行的合法所有者。"""
    for _ in range(15):
        await asyncio.sleep(0)


def command_ids(cues):
    """同一身份可能多次运输，验收按真实可执行指令身份计数而不把状态更新算成重演。"""
    return {cue.gesture_id for cue in cues if cue.gesture_source == "command"}


def assert_released(controller, decider, http):
    """检查两层实际持有资源，而非仅凭 close 返回就宣称没有悬挂任务。"""
    assert not controller._owned_tasks and not decider._queries
    assert decider._client is None and decider._user_timeout is None
    assert http.clients[0].closes == 1
    for task in (
        controller._prepare_task,
        controller._user_task,
        decider._warm_task,
        decider._user_task,
    ):
        assert task is None or task.done()
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("xiaoya-")
    ]


async def test_blocked_head_does_not_block_real_command_cue(http):
    """应用仍拥有预热任务，但官网响应头完全未到也必须及时采纳真实 POST 的动作证据。"""
    warm = held_head(http)
    http.request_responses.append(command_response())
    decider = http.decider()
    controller = PresentationController(decider)
    cues = []
    controller.attach(cues.append)
    try:
        controller.prepare()
        controller.complete_turn("挥挥手")
        await asyncio.wait_for(warm.enter_started.wait(), 0.5)
        await asyncio.wait_for(controller._user_task, 0.5)
        assert decider._warm_task is not None and not decider._warm_task.done()
        cue = controller._cue
        assert cue.gesture_source == "command" and cue.intent.gesture == "wave"
        assert cue.phase == "thinking" and not cue.reply_id and not cue.segment_id
        assert len(command_ids(cues)) == len(http.request_calls) == 1
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "started")
        assert controller.acknowledge(cue.turn_id, cue.gesture_id, "completed")
        assert controller._last_command == "wave"
        assert controller._cue.command_feedback == "none"
    finally:
        await controller.aclose()
    assert_released(controller, decider, http)


@pytest.mark.parametrize("stage", ["warmup", "http"])
async def test_stable_listening_final_upgrade_survives_short_deadline_once(http, stage):
    """先以真实VAD许可的稳定partial启动请求；最终同全文仍只升级一个POST及指令身份。"""
    if stage == "warmup":
        held_head(http)
    release = asyncio.Event()
    response = command_response("shake", wait=release)
    started = response.content.started
    http.request_responses.append(response)
    decider = http.decider()
    controller = PresentationController(decider)
    cues = []
    controller.attach(cues.append)
    try:
        if stage == "warmup":
            controller.prepare()
        controller.user_speaking(True)
        controller.transcribe("摇摇头", final=False)
        await asyncio.wait_for(started.wait(), 1.0)
        owned_user = decider._user_task
        old_waiter = controller._user_task
        controller.complete_turn("摇摇头")
        await flush()
        assert decider._user_task is owned_user and decider._user_complete
        assert old_waiter.cancelled() and controller._user_task is not old_waiter
        await asyncio.sleep(1.25)
        assert not owned_user.done() and not controller._user_task.done()
        release.set()
        await asyncio.wait_for(controller._user_task, 0.5)
        cue = controller._cue
        assert cue.gesture_source == "command" and cue.intent.gesture == "shake"
        assert len(command_ids(cues)) == len(http.request_calls) == 1
        assert len(http.warm_calls) == (1 if stage == "warmup" else 0)
        assert controller._cue.command_feedback == "none"
    finally:
        release.set()
        await controller.aclose()
    assert_released(controller, decider, http)


@pytest.mark.parametrize("stage", ["warmup", "http"])
async def test_total_deadline_reports_retry_and_late_release_cannot_execute(
    http, monkeypatch, stage
):
    """正式 POST 超时必须反馈未知且不重演；旁路 HEAD 迟到完成也不能复活已过期指令。"""
    timing = SimpleNamespace(
        user_total=0.03, reply_total=3.5, listening_total=1.2, application_grace=0.2
    )
    monkeypatch.setattr(jev, "PRESENTATION_TIMING", timing)
    monkeypatch.setattr(presentation, "PRESENTATION_TIMING", timing)
    if stage == "warmup":
        warm = held_head(http)
    release = asyncio.Event()
    response = command_response(wait=release)
    started = response.content.started
    http.request_responses.append(response)
    decider = http.decider()
    controller = PresentationController(decider)
    cues = []
    controller.attach(cues.append)
    try:
        if stage == "warmup":
            controller.prepare()
        controller.complete_turn("挥挥手")
        await asyncio.wait_for(started.wait(), 0.5)
        await asyncio.wait_for(controller._user_task, 0.5)
        assert controller._cue.command_feedback == "retry"
        assert controller._prohibited == "all" and controller._command is None
        assert "本次动作状态：unavailable。" in controller.command_context()
        assert decider._user_task.done() and not decider._queries
        posts_at_expiry = len(http.request_calls)
        release.set()
        if stage == "warmup":
            warm.enter_wait.set()
        await decider.warmup()
        await flush()
        assert len(http.request_calls) == posts_at_expiry == 1
        assert not command_ids(cues) and controller._cue.command_feedback == "retry"
        assert response.exits == 1
    finally:
        release.set()
        await controller.aclose()
    assert_released(controller, decider, http)


@pytest.mark.parametrize("stage", ["warmup", "http"])
async def test_close_cancels_owned_warm_and_http_without_late_commands(http, stage):
    """挂断同时回收独立进行的免费 HEAD 与正式 POST；释放任一旧等待都不能再发动作。"""
    if stage == "warmup":
        held_head(http)
    release = asyncio.Event()
    response = command_response(wait=release)
    started = response.content.started
    http.request_responses.append(response)
    decider = http.decider()
    controller = PresentationController(decider)
    cues = []
    controller.attach(cues.append)
    try:
        controller.prepare()
        controller.complete_turn("挥挥手")
        await asyncio.wait_for(started.wait(), 0.5)
        await controller.aclose()
        release.set()
        await flush()
        assert not command_ids(cues)
        assert len(http.request_calls) == 1
        assert len(http.warm_calls) == 1 and decider._user_result is None
        assert response.exits == 1
    finally:
        release.set()
        await controller.aclose()
    assert_released(controller, decider, http)
