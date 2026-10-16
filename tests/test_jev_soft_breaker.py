"""真实Jev适配器按优先级隔离软故障，供应商明确限流与鉴权仍作用于整个会话。"""

import asyncio
from dataclasses import replace

import aiohttp
import pytest
from test_jev import Response, context, response_body
from test_jev import http as http

from xiaoya.domain.presentation import choose_user_command
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING


@pytest.fixture
def decider(http):
    """复用既有内存HTTP边界执行真实类，不创建额外服务或读取运行凭据。"""
    return http.decider()


def user_context(index: int, *, listening: bool = False):
    """每次明确提交都具备独立身份，防止将正确的同输入失败缓存误判为跨轮熔断。"""
    return replace(context("listening" if listening else "thinking"), turn_id=f"turn-{index}")


def command_response() -> Response:
    """强证据指令须经过真实解析及原.85/.90域门，不把None或假返回值当作恢复成功。"""
    body = response_body()
    answer = body["answers"]["requested_gesture"]
    answer["choice"] = "wave"
    answer["probabilities"] = {key: float(key == "wave") for key in answer["probabilities"]}
    answer["confidence"] = 1.0
    return Response(body=body)


async def query(decider, mode: str, index: int):
    """区分回复、倾听与完整提交的真实准入，不直接改计数器构造冷却结果。"""
    return (
        await decider.decide(context())
        if mode == "reply"
        else await decider.decide_user(user_context(index, listening=mode == "listening"))
    )


def queue(http, mode: str, *responses: Response) -> None:
    """回复与用户HTTP队列分别注入，是否真正POST由生产适配器决定。"""
    (http.responses if mode == "reply" else http.request_responses).extend(responses)


@pytest.mark.asyncio
async def test_reply_soft_timeouts_do_not_suppress_fresh_complete_command(http, decider) -> None:
    """复现04直接路径：三个低优先级回复截止不能令新完整动作输入零POST失效。"""
    queue(http, "reply", *(Response(error=TimeoutError()) for _ in range(3)))
    for _ in range(3):
        assert await decider.decide(context()) is None
    queue(http, "user", command_response())
    try:
        result = await decider.decide_user(user_context(1))
        assert result is not None
        assert choose_user_command(result) == "wave"
        assert len(http.calls) == 3 and len(http.request_calls) == 1
    finally:
        await decider.aclose()


@pytest.mark.parametrize(
    ("listening", "error_type"),
    [
        (False, TimeoutError),
        (False, aiohttp.ClientConnectionError),
        (True, aiohttp.ClientConnectionError),
    ],
)
@pytest.mark.asyncio
async def test_fresh_complete_input_has_one_attempt_during_user_soft_cooldown(
    http, decider, listening, error_type
) -> None:
    """完整提交是新的尝试授权，不能因旧软故障静默丢弃；增量及同身份终态仍不可重放。"""
    queue(http, "user", *(Response(error=error_type()) for _ in range(3)), command_response())
    try:
        for index in range(3):
            assert await decider.decide_user(user_context(index, listening=listening)) is None
        assert decider._soft_cooldown["user"] > http.clock.now
        assert await decider.decide_user(user_context(4, listening=True)) is None
        assert len(http.request_calls) == 3
        if not listening:
            # 已失败的完整身份即使被再次提交，也不是新输入授权。
            assert await decider.decide_user(user_context(2)) is None
            assert len(http.request_calls) == 3
        final = user_context(2 if listening else 5)
        values = await asyncio.gather(*(decider.decide_user(final) for _ in range(4)))
        assert all(value is values[0] and value is not None for value in values)
        assert choose_user_command(values[0]) == "wave"
        assert len(http.request_calls) == 4
        assert http.request_calls[-1][1]["timeout"].total == PRESENTATION_TIMING.user_total
        assert not decider._queries
        assert await decider.decide_user(user_context(6, listening=True)) is None
        assert len(http.request_calls) == 4
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_mixed_reply_and_user_failures_do_not_form_one_failure_streak(http, decider) -> None:
    """实测混合Timeout序列不能跨优先级累计第三击，两个通道分别保持原故障阈值。"""
    queue(http, "reply", Response(error=TimeoutError()), Response(error=TimeoutError()))
    queue(http, "user", Response(error=TimeoutError()), command_response())
    try:
        assert await query(decider, "reply", 0) is None
        assert await query(decider, "user", 1) is None
        assert await query(decider, "reply", 0) is None
        result = await query(decider, "user", 2)
        assert result is not None and choose_user_command(result) == "wave"
        assert len(http.request_calls) == 2
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_user_soft_failures_do_not_suppress_reply(http, decider) -> None:
    """反向也隔离，用户通道三次失败不该直接抹掉仍可工作的普通表情通道。"""
    queue(http, "user", *(Response(error=TimeoutError()) for _ in range(3)))
    queue(http, "reply", Response())
    try:
        for index in range(3):
            assert await query(decider, "user", index) is None
        assert await query(decider, "reply", 10) is not None
        assert len(http.calls) == 1
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_other_lane_success_does_not_erase_own_failure_streak(http, decider) -> None:
    """只拆检查点不够，另一通道成功若仍清共享计数，就会让故障通道失去原防风暴保护。"""
    queue(http, "reply", *(Response(error=TimeoutError()) for _ in range(3)), Response())
    queue(http, "user", command_response())
    try:
        for _ in range(2):
            assert await query(decider, "reply", 0) is None
        assert await query(decider, "user", 1) is not None
        assert await query(decider, "reply", 0) is None
        assert await query(decider, "reply", 0) is None
        assert len(http.calls) == 3
    finally:
        await decider.aclose()


@pytest.mark.parametrize("mode", ["reply", "listening"])
@pytest.mark.asyncio
async def test_opportunistic_lanes_retain_three_failures_thirty_second_guard(
    http, decider, mode
) -> None:
    """回复与增量倾听仍三次故障冷却30秒；新的完整提交由独立用例验证一次尝试。"""
    queue(
        http,
        mode,
        *(Response(error=aiohttp.ClientConnectionError()) for _ in range(3)),
        command_response(),
    )
    try:
        for index in range(3):
            assert await query(decider, mode, index) is None
        assert await query(decider, mode, 4) is None
        calls = http.calls if mode == "reply" else http.request_calls
        assert len(calls) == 3
        http.clock.now += 30
        assert await query(decider, mode, 5) is not None
        assert len(calls) == 4
    finally:
        await decider.aclose()


@pytest.mark.parametrize("status", [429, 529])
@pytest.mark.parametrize("source", ["reply", "user"])
@pytest.mark.parametrize("target", ["reply", "user"])
@pytest.mark.asyncio
async def test_explicit_retry_after_remains_shared_across_all_lanes(
    http, decider, status, source, target
) -> None:
    """真实供应商Retry-After不能因优先级被绕过，期满只接受新输入，不自动重放受限调用。"""
    queue(http, source, Response(status, headers={"Retry-After": "12"}))
    try:
        assert await query(decider, source, 1) is None
        queue(http, target, command_response())
        assert await query(decider, target, 2) is None
        assert len(http.calls) + len(http.request_calls) == 1
        http.clock.now += 11.99
        assert await query(decider, target, 3) is None
        http.clock.now += 0.01
        assert await query(decider, target, 4) is not None
        assert len(http.calls) + len(http.request_calls) == 2
    finally:
        await decider.aclose()


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("source", ["reply", "user"])
@pytest.mark.asyncio
async def test_auth_disables_both_lanes_for_entire_session(http, decider, status, source) -> None:
    """鉴权是会话配置错误，不能拆成某通道稍后再尝试而继续使用无效密钥付费调用。"""
    queue(http, source, Response(status))
    try:
        assert await query(decider, source, 1) is None
        http.clock.now += 1000
        assert await query(decider, "reply", 2) is None
        assert await query(decider, "user", 3) is None
        assert len(http.calls) + len(http.request_calls) == 1
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_listening_short_timeouts_still_do_not_count_as_failures(http, decider) -> None:
    """原短窗过期是可选增强失效，不能累积用户软冷却并阻断稍后的完整动作输入。"""
    queue(http, "user", *(Response(error=TimeoutError()) for _ in range(3)), command_response())
    try:
        for index in range(3):
            assert await decider.decide_user(user_context(index, listening=True)) is None
        result = await query(decider, "user", 4)
        assert result is not None and choose_user_command(result) == "wave"
        assert len(http.request_calls) == 4
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_same_complete_input_is_one_post_and_failed_input_is_not_retried(
    http, decider
) -> None:
    """分通道不能把失败缓存改成重试，也不能因多个等待者而对同一完整输入重复付费。"""
    queue(http, "user", command_response(), Response(error=TimeoutError()))
    try:
        values = await asyncio.gather(*(decider.decide_user(user_context(1)) for _ in range(4)))
        assert all(value is values[0] and value is not None for value in values)
        assert len(http.request_calls) == 1
        assert await query(decider, "user", 2) is None
        assert await query(decider, "user", 2) is None
        assert len(http.request_calls) == 2
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_budget_and_close_cleanup_remain_unchanged(http, decider) -> None:
    """固定8/3.5/1.2及关闭回收不因熔断提案改变，释放假HTTP后不能出现迟到POST或结果。"""
    assert (
        PRESENTATION_TIMING.user_total,
        PRESENTATION_TIMING.reply_total,
        PRESENTATION_TIMING.listening_total,
    ) == (8.0, 3.5, 1.2)
    reply = Response(wait=asyncio.Event())
    user = Response(wait=asyncio.Event())
    queue(http, "reply", reply)
    queue(http, "user", user)
    reply_task = asyncio.create_task(query(decider, "reply", 0))
    user_task = asyncio.create_task(query(decider, "user", 1))
    await asyncio.wait_for(
        asyncio.gather(reply.content.started.wait(), user.content.started.wait()), 1
    )
    assert http.calls[0][1]["timeout"].total == 3.5
    assert http.request_calls[0][1]["timeout"].total == 8.0
    await decider.aclose()
    await asyncio.gather(reply_task, user_task, return_exceptions=True)
    assert not decider._queries and decider._client is None
    assert reply.exits == user.exits == 1
    assert http.clients[0].closes == 1
    assert await query(decider, "user", 2) is None
    assert await query(decider, "reply", 0) is None
    assert len(http.calls) == len(http.request_calls) == 1


@pytest.mark.parametrize("blocked", ["rate", "auth", "closed"])
@pytest.mark.asyncio
async def test_actual_post_rechecks_explicit_shared_provider_permission(
    http, decider, blocked
) -> None:
    """入口检查后到任务调度前仍可能收到另一请求的明确限流，真正付费前必须再核共享许可。"""
    decider._ensure_client()
    if blocked == "rate":
        decider._cooldown = http.clock.now + 10
    elif blocked == "auth":
        decider._disabled = True
    else:
        decider._closed = True
    queue(http, "reply", Response())
    try:
        assert (
            await decider._post(
                decider._request(context()), 3.5, deadline=asyncio.get_running_loop().time() + 3.5
            )
            is None
        )
        assert len(http.calls) == 0
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_inflight_success_and_shorter_retry_after_cannot_shorten_existing_global_hold(
    http, decider
) -> None:
    """并行已发请求可依次返回成功和更短限流头，但已有更长Retry-After仍必须对全会话有效。"""
    late_limit = Response(429, headers={"Retry-After": "5"})
    late_limit.enter_wait = asyncio.Event()
    late_limit.enter_started = asyncio.Event()
    late_success = Response(wait=asyncio.Event())
    queue(http, "reply", late_limit, late_success)
    queue(http, "user", Response(529, headers={"Retry-After": "30"}))
    limited = asyncio.create_task(query(decider, "reply", 0))
    successful = asyncio.create_task(query(decider, "reply", 0))
    await asyncio.wait_for(
        asyncio.gather(late_limit.enter_started.wait(), late_success.content.started.wait()), 1
    )
    try:
        assert await query(decider, "user", 1) is None
        expected = http.clock.now + 30
        late_success.content.wait.set()
        assert await successful is not None
        late_limit.enter_wait.set()
        assert await limited is None
        assert decider._cooldown >= expected
    finally:
        late_limit.enter_wait.set()
        late_success.content.wait.set()
        await decider.aclose()
        await asyncio.gather(limited, successful, return_exceptions=True)
