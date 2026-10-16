"""覆盖倾听截止已经触发、HTTP 尚在异步退出时到达最终输入的熔断归属。"""

import asyncio
from types import SimpleNamespace

import pytest
from test_jev import Harness, Response, context, response_body

from xiaoya.domain.presentation import choose_user_command
from xiaoya.infrastructure import jev


class SlowExitResponse(Response):
    """传输层清理可以异步，截止回调触发不代表旧任务已经进入异常分类。"""

    def __init__(self):
        """暂停正文使短窗真实到期，再单独暂停退出以暴露最终输入竞争窗口。"""
        super().__init__(wait=asyncio.Event())
        self.exit_started = asyncio.Event()
        self.exit_release = asyncio.Event()

    async def __aexit__(self, *args):
        """保留实际取消传播，只有外部释放清理后才允许生产适配器分类 Timeout。"""
        self.exit_started.set()
        await self.exit_release.wait()
        await super().__aexit__(*args)


@pytest.fixture
def http(monkeypatch):
    """封住唯一 HTTP 工厂并缩短计时，仍执行真实预算、错误计数和供应商响应解析。"""
    harness = Harness()
    monkeypatch.setattr(jev.aiohttp, "ClientSession", harness.client)
    monkeypatch.setattr(jev, "time", harness.clock)
    monkeypatch.setattr(
        jev,
        "PRESENTATION_TIMING",
        SimpleNamespace(user_total=0.5, listening_total=0.03, reply_total=0.5),
    )
    return harness


async def test_expired_listening_cleanup_does_not_count_as_final_failure(http):
    """短窗清理仍允许最终指令，用户成功不清回复原两次故障，第三次回复故障后才单独冷却。"""
    http.responses.extend([Response(500), Response(500), Response(500), Response()])
    old = SlowExitResponse()
    body = response_body()
    body["answers"]["requested_gesture"].update(
        choice="wave",
        confidence=1.0,
        probabilities={name: float(name == "wave") for name in jev.COMMAND_LABELS},
    )
    http.request_responses.extend([old, Response(body=body)])
    decider = http.decider()
    callers = []
    try:
        assert await decider.decide(context()) is None
        assert await decider.decide(context()) is None
        assert len(http.calls) == 2
        partial = asyncio.create_task(decider.decide_user(context("listening")))
        callers.append(partial)
        await asyncio.wait_for(old.exit_started.wait(), 0.5)
        assert decider._user_timeout.expired()
        final = asyncio.create_task(decider.decide_user(context("thinking")))
        callers.append(final)
        await asyncio.sleep(0)
        old.exit_release.set()
        partial_result, final_result = await asyncio.gather(partial, final)
        assert partial_result is None
        assert final_result is not None
        assert choose_user_command(final_result) == "wave"
        assert await decider.decide(context()) is None
        assert await decider.decide(context()) is None
        assert len(http.calls) == 3
        assert len(http.request_calls) == 2 and old.exits == 1
        assert not http.warm_calls and not decider._queries
    finally:
        old.exit_release.set()
        for caller in callers:
            if not caller.done():
                caller.cancel()
        await asyncio.gather(*callers, return_exceptions=True)
        await decider.aclose()
