"""离线核对 OpenRouter Decisions、独立代理与会话降级；不读取真实凭据。"""

import asyncio
import copy
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import aiohttp
import pytest

from xiaoya.domain.delivery import GESTURE_LABELS, STYLE_LABELS
from xiaoya.domain.presentation import (
    PresentationContext,
    choose_presentation,
    choose_user_command,
    choose_user_prohibition,
)
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING
from xiaoya.infrastructure import jev


def response_body() -> dict[str, Any]:
    """使用实测的解析模型名；非 none 概率与分类置信度分开，Score 为加权等级。"""
    return {
        "model": jev.JEV_RESPONSE_MODEL,
        "answers": {
            "requested_gesture": {
                "type": "choice",
                "choice": "none",
                "probabilities": {name: float(name == "none") for name in jev.COMMAND_LABELS},
                "confidence": 1.0,
            },
            "prohibited_gesture": {
                "type": "choice",
                "choice": "none",
                "probabilities": {name: float(name == "none") for name in jev.PROHIBITION_LABELS},
                "confidence": 1.0,
            },
            "style": {
                "type": "choice",
                "choice": "happy",
                "probabilities": {
                    name: 0.9 if name == "happy" else 0.1 if name == "neutral" else 0
                    for name in STYLE_LABELS
                },
                "confidence": 0.88,
            },
            "gesture": {
                "type": "choice",
                "choice": "wave",
                "probabilities": {
                    name: 0.95 if name == "wave" else 0.05 if name == "none" else 0
                    for name in GESTURE_LABELS
                },
                "confidence": 0.92,
            },
            "energy": {
                "type": "score",
                "score": 1.05,
                "probabilities": {"0": 0, "1": 0.95, "2": 0.05},
                "legend": {"0": "安静", "1": "自然", "2": "兴奋"},
                "confidence": 0.92,
            },
        },
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def context(phase: str = "speaking") -> PresentationContext:
    """真实领域身份保留中文原文，测试不引入正文控制头或依赖 SDK 对象。"""
    return PresentationContext(
        phase=phase,
        turn_id="user-turn",
        revision=1,
        user_text="你好，挥挥手吧。",
        text="你好呀，很高兴见到你。",
        reply_id="reply-a" if phase == "speaking" else "",
        segment_id="segment-a" if phase == "speaking" else "",
    )


@dataclass
class Clock:
    """只替换适配器的时间端口，事件循环的真实时钟继续正常处理取消。"""

    now: float = 100.0
    epoch: float = 1_730_000_000.0

    def monotonic(self) -> float:
        """退避推进由测试直接控制，避免用实际等待掩盖调度竞态。"""
        return self.now

    def time(self) -> float:
        """HTTP 日期按固定 UTC 墙钟解析，不依赖测试主机时区。"""
        return self.epoch


class Content:
    """分块和等待属于 HTTP 端口，生产 JSON 解析与失败策略照常执行。"""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        wait: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        """允许暂停读取或中途断流，以验证取消和关闭而不是只模拟立即响应。"""
        self.chunks = chunks
        self.wait = wait
        self.error = error
        self.started = asyncio.Event()
        self.aborted = False
        self.reads = 0

    async def iter_chunked(self, size: int):
        """网络可能在 UTF-8 字符中间拆包，消费方必须累积字节后一次解析。"""
        assert size == 8192
        self.started.set()
        if self.wait is not None:
            await self.wait.wait()
        if self.aborted:
            raise aiohttp.ClientConnectionError("不应暴露的网络响应内容")
        if self.error:
            raise self.error
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def abort(self) -> None:
        """关闭连接令等待中的读取退出，使测试能发现结束后的悬挂请求。"""
        self.aborted = True
        if self.wait is not None:
            self.wait.set()


class Response:
    """HTTP 状态、头部和字节流独立注入，生产状态分支不能被测试替换。"""

    def __init__(
        self,
        status: int = 200,
        *,
        body: Any = None,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        wait: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        """默认提供合法官方响应，异常场景只改变本次 HTTP 结果。"""
        self.status = status
        self.headers = headers or {}
        encoded = json.dumps(response_body() if body is None else body, ensure_ascii=False).encode()
        self.content = Content(chunks if chunks is not None else [encoded], wait=wait, error=error)
        self.exits = 0

    async def __aenter__(self):
        """可在响应头前暂停或失败，区分可选 HEAD 的连通性与正式 POST 读取。"""
        if hasattr(self, "enter_error"):
            raise self.enter_error
        if hasattr(self, "enter_wait"):
            self.enter_started.set()
            await self.enter_wait.wait()
        return self

    async def __aexit__(self, *_: object) -> None:
        """取消也必须离开响应上下文，不能因异常吞掉而遗留读取资源。"""
        self.exits += 1


class Client:
    """连接池只记录外部调用，创建、复用与幂等关闭仍由生产适配器控制。"""

    def __init__(self, harness: "Harness", options: dict[str, Any]) -> None:
        """保留构造参数验证独立 Bearer 密钥及默认超时，不向外发出网络请求。"""
        self.harness = harness
        self.options = options
        self.closes = 0
        self.responses: list[Response] = []

    def post(self, url: str, **kwargs: Any) -> Response:
        """两种独立输入各有响应队列，不能用同一次模拟掩盖输入隔离、缓存或取消。"""
        assert not self.closes
        if "requested_gesture" in kwargs["json"]["questions"]:
            self.harness.request_calls.append((url, kwargs))
            response = (
                self.harness.request_responses.popleft()
                if self.harness.request_responses
                else Response()
            )
        else:
            self.harness.calls.append((url, kwargs))
            response = self.harness.responses.popleft()
        self.responses.append(response)
        return response

    def head(self, url: str, **kwargs: Any) -> Response:
        """免费预热使用独立队列，不消耗任何语义响应；保持代理和超时可观察。"""
        self.harness.warm_calls.append((url, kwargs))
        response = (
            self.harness.warm_responses.popleft() if self.harness.warm_responses else Response()
        )
        self.responses.append(response)
        return response

    async def close(self) -> None:
        """关闭连接同时终止挂住的响应，端口不承担重新启动或重试行为。"""
        self.closes += 1
        for response in self.responses:
            response.content.abort()


@dataclass
class Harness:
    """每项测试独立持有请求队列、连接池与时间，两个房间不会共享熔断状态。"""

    responses: deque[Response] = field(default_factory=deque)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    request_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    request_responses: deque[Response] = field(default_factory=deque)
    warm_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    warm_responses: deque[Response] = field(default_factory=deque)
    clients: list[Client] = field(default_factory=list)
    clock: Clock = field(default_factory=Clock)

    def client(self, **kwargs: Any) -> Client:
        """工厂保留每次分配，便于证明整个会话只创建一个连接池。"""
        instance = Client(self, kwargs)
        self.clients.append(instance)
        return instance

    def decider(self) -> jev.JevPresentationDecider:
        """假密钥与显式代理隔离真实服务，构造自身不分配网络资源。"""
        return jev.JevPresentationDecider(
            base_url="https://openrouter.ai/",
            model="typesafe/jev-1.13",
            api_key="jev-test-secret",
            proxy_url="http://127.0.0.1:10808",
        )


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """替换唯一网络与时间边界，测试执行真实请求生成、解析和生命周期。"""
    harness = Harness()
    monkeypatch.setattr(jev.aiohttp, "ClientSession", harness.client)
    monkeypatch.setattr(jev, "time", harness.clock)
    return harness


@pytest.mark.asyncio
async def test_batch_contract_reuses_connection_and_keeps_chinese_text(http: Harness) -> None:
    """显式预热和判断共用连接池，但正式请求不隐式依赖官网；不继承环境或泄露身份。"""
    decider = http.decider()
    assert http.clients == []
    await decider.warmup()
    http.responses.extend([Response(), Response()])
    first = await decider.decide(context())
    await decider.decide(context("listening"))
    assert first is not None
    assert first.intent.style == "happy"
    assert first.intent.gesture == "wave"
    assert first.energy == pytest.approx(0.525)
    assert first.perform_probability == pytest.approx(0.95)
    assert len(http.clients) == 1
    assert http.clients[0].options["headers"] == {"Authorization": "Bearer jev-test-secret"}
    assert http.clients[0].options["timeout"].total == PRESENTATION_TIMING.user_total
    assert http.clients[0].options["trust_env"] is False
    assert len(http.warm_calls) == 1
    assert http.warm_calls[0][0] == "https://openrouter.ai/"
    assert http.warm_calls[0][1]["proxy"] == "http://127.0.0.1:10808"
    assert http.warm_calls[0][1]["timeout"].total == 5
    assert http.warm_calls[0][1]["allow_redirects"] is False
    url, request = http.calls[0]
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert request["allow_redirects"] is False
    assert request["proxy"] == "http://127.0.0.1:10808"
    assert 0 < request["timeout"].total <= PRESENTATION_TIMING.reply_total
    body = request["json"]
    assert len(http.request_calls) == 0
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"]["user"] == "你好，挥挥手吧。"
    assert body["state"]["assistant"] == "你好呀，很高兴见到你。"
    assert body["state"]["previous_user_message"] == ""
    assert body["state"]["previous_assistant_message"] == ""
    assert {name: value["type"] for name, value in body["questions"].items()} == {
        "style": "choice",
        "gesture": "choice",
        "energy": "score",
    }
    assert set(body["questions"]["style"]["criteria"]) == set(STYLE_LABELS)
    assert set(body["questions"]["gesture"]["criteria"]) == set(GESTURE_LABELS)
    assert len(body["questions"]["energy"]["criteria"]) == 3
    assert "jev-test-secret" not in json.dumps(body)
    assert "user-turn" not in json.dumps(body)
    assert 0 < http.calls[1][1]["timeout"].total <= PRESENTATION_TIMING.listening_total
    assert http.calls[1][1]["json"]["state"]["assistant"] == ""
    await decider.aclose()
    await decider.aclose()
    assert http.clients[0].closes == 1
    assert await decider.decide(context()) is None
    assert len(http.calls) == 2


@pytest.mark.asyncio
async def test_expired_decision_preserves_free_warmup_and_close_cancels_it(http: Harness) -> None:
    """独立预热不受单个正文取消影响；业务无需等 HEAD，但挂断必须回收二者。"""
    warm = Response()
    warm.enter_wait = asyncio.Event()
    warm.enter_started = asyncio.Event()
    http.warm_responses.append(warm)
    response = Response(wait=asyncio.Event())
    http.responses.extend([response, Response()])
    decider = http.decider()
    warming = asyncio.create_task(decider.warmup())
    await warm.enter_started.wait()
    expired = asyncio.create_task(decider.decide(context()))
    await response.content.started.wait()
    expired.cancel()
    with pytest.raises(asyncio.CancelledError):
        await expired
    assert decider._warm_task is not None and not decider._warm_task.done()
    assert await decider.decide(context()) is not None
    warm.enter_wait.set()
    await warming
    assert len(http.warm_calls) == 1
    await decider.aclose()

    another_warm = Response()
    another_warm.enter_wait = asyncio.Event()
    another_warm.enter_started = asyncio.Event()
    http.warm_responses.append(another_warm)
    other = http.decider()
    pending = asyncio.create_task(other.warmup())
    await another_warm.enter_started.wait()
    await other.aclose()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert other._warm_task is not None and other._warm_task.cancelled()
    assert http.clients[-1].closes == 1
    assert await other.decide(context()) is None


@pytest.mark.asyncio
async def test_lost_proxy_connection_allows_new_input_without_replaying(http: Harness) -> None:
    """真实 POST 断流仍记录故障；新输入复用连接池自行连通，不隐式重放或等待官网。"""
    http.responses.extend([Response(error=aiohttp.ClientConnectionError()), Response()])
    decider = http.decider()
    assert await decider.decide(context()) is None
    assert len(http.calls) == 1
    assert await decider.decide(context()) is not None
    assert not http.warm_calls
    assert len(http.calls) == 2
    await decider.aclose()


@pytest.mark.asyncio
async def test_utf8_chunked_body_and_maximum_body_boundary(http: Harness) -> None:
    """多字节中文拆包和恰好上限仍合法，保护措施不能误拒正确的小型协议响应。"""
    raw = json.dumps(response_body(), ensure_ascii=False).encode()
    fragmented = [raw[index : index + 7] for index in range(0, len(raw), 7)]
    padded = raw + b" " * (65536 - len(raw))
    http.responses.extend(
        [
            Response(chunks=fragmented),
            Response(
                chunks=[padded[index : index + 8192] for index in range(0, len(padded), 8192)]
            ),
        ]
    )
    decider = http.decider()
    assert await decider.decide(context()) is not None
    assert await decider.decide(context()) is not None
    await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_failure_disables_only_this_session(
    http: Harness, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    """认证失败只停用当前增强，一次脱敏诊断之后不重试，也不读取服务端错误正文。"""
    denied = Response(status, chunks=[b"jev-test-secret sensitive text"])
    http.responses.extend([denied, Response()])
    first = http.decider()
    assert await first.decide(context()) is None
    assert await first.decide(context()) is None
    assert len(http.calls) == 1
    assert denied.content.reads == 0
    assert caplog.text.count("鉴权失败") == 1
    assert "jev-test-secret" not in caplog.text
    assert "sensitive" not in caplog.text
    second = http.decider()
    assert await second.decide(context()) is not None
    assert len(http.clients) == 2
    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 529])
@pytest.mark.parametrize("header", ["4", "http-date", "invalid", "NaN", None])
async def test_retry_after_uses_bounded_delay_without_immediate_retry(
    http: Harness, status: int, header: str | None
) -> None:
    """秒数和 HTTP 日期均退避，损坏头使用一秒默认值且不重放已经过期的请求。"""
    delay = 4 if header in {"4", "http-date"} else 1
    if header == "http-date":
        header = format_datetime(datetime.fromtimestamp(http.clock.epoch + 4, UTC), usegmt=True)
    headers = {} if header is None else {"Retry-After": header}
    limited = Response(status, headers=headers)
    http.responses.extend([limited, Response()])
    decider = http.decider()
    assert await decider.decide(context()) is None
    assert await decider.decide(context()) is None
    http.clock.now += delay - 0.01
    assert await decider.decide(context()) is None
    assert len(http.calls) == 1
    assert limited.content.reads == 0
    http.clock.now += 0.02
    assert await decider.decide(context()) is not None
    assert len(http.calls) == 2
    await decider.aclose()


@pytest.mark.asyncio
async def test_three_failures_cool_down_then_recover(http: Harness) -> None:
    """连续三次故障触发三十秒熔断，到期只执行最新调用且沿用原连接池。"""
    http.responses.extend([Response(500), Response(500), Response(500), Response()])
    decider = http.decider()
    for _ in range(3):
        assert await decider.decide(context()) is None
    assert await decider.decide(context()) is None
    http.clock.now += 29.99
    assert await decider.decide(context()) is None
    assert len(http.calls) == 3
    http.clock.now += 0.02
    assert await decider.decide(context()) is not None
    assert len(http.calls) == 4
    assert len(http.clients) == 1
    await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 529])
async def test_repeated_rate_limits_back_off_and_reset_after_success(
    http: Harness, status: int
) -> None:
    """持续拥堵不能按每秒不断重试；成功后恢复短预算，服务器更长的退避仍有优先权。"""
    http.responses.extend(
        [
            Response(status),
            Response(status),
            Response(status, headers={"Retry-After": "60"}),
            Response(),
            Response(status),
        ]
    )
    decider = http.decider()
    for delay in (1, 2, 60):
        assert await decider.decide(context()) is None
        http.clock.now += delay - 0.01
        before = len(http.calls)
        assert await decider.decide(context()) is None
        assert len(http.calls) == before
        http.clock.now += 0.02
    assert await decider.decide(context()) is not None
    assert await decider.decide(context()) is None
    assert decider._cooldown == pytest.approx(http.clock.now + 1)
    assert len(http.calls) == 5 and len(http.clients) == 1
    await decider.aclose()


@pytest.mark.asyncio
async def test_success_resets_consecutive_failure_count(http: Harness) -> None:
    """一次合法响应打断故障序列，间歇性错误不能被累计成永久不可用。"""
    http.responses.extend([Response(500), Response(500), Response()] * 2)
    decider = http.decider()
    for index in range(6):
        decision = await decider.decide(context())
        assert (decision is not None) == (index % 3 == 2)
    assert len(http.calls) == 6
    await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "stream", "oversize", "bad-json", "redirect"])
async def test_transport_failures_degrade_without_retry_or_secret_leak(
    http: Harness, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """超时、断流与损坏协议只取消增强；一次请求的错误不得触发隐藏重试或日志泄密。"""
    response = {
        "timeout": Response(error=TimeoutError("jev-test-secret timeout body")),
        "stream": Response(error=aiohttp.ClientPayloadError("jev-test-secret stream body")),
        "oversize": Response(chunks=[b"x" * 8192] * 9),
        "bad-json": Response(chunks=[b'{"secret":"jev-test-secret"']),
        "redirect": Response(302, headers={"Location": "https://other.invalid"}),
    }[failure]
    http.responses.extend([response, Response()])
    decider = http.decider()
    assert await decider.decide(context()) is None
    assert len(http.calls) == 1
    assert response.exits == 1
    assert "jev-test-secret" not in caplog.text
    assert await decider.decide(context()) is not None
    await decider.aclose()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_does_not_count_as_service_failure(http: Harness) -> None:
    """取消后再发生两次真实故障仍可查询，行为证明CancelledError不计入第三击而不依赖私有字段。"""
    response = Response(wait=asyncio.Event())
    http.responses.extend([response, Response(500), Response(500), Response()])
    decider = http.decider()
    task = asyncio.create_task(decider.decide(context()))
    await response.content.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.exits == 1
    assert await decider.decide(context()) is None
    assert await decider.decide(context()) is None
    assert await decider.decide(context()) is not None
    assert len(http.calls) == 4 and len(http.clients) == 1
    await decider.aclose()


@pytest.mark.asyncio
async def test_close_while_reading_releases_wait_and_prevents_new_client(http: Harness) -> None:
    """挂断并行于网络读取时仍须释放连接，迟到故障不允许新建客户端复活会话。"""
    response = Response(wait=asyncio.Event())
    http.responses.append(response)
    decider = http.decider()
    task = asyncio.create_task(decider.decide(context()))
    await response.content.started.wait()
    await decider.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    await decider.aclose()
    assert await decider.decide(context()) is None
    assert http.clients[0].closes == 1
    assert response.exits == 1
    assert len(http.clients) == 1


@pytest.mark.parametrize("confidence,expected", [(0.649, 0.5), (0.65, 0.525)])
def test_score_is_weighted_level_and_perform_uses_action_probability(
    confidence: float, expected: float
) -> None:
    """强度按量表归一化；动作门槛使用非 none 概率，分类置信度仍独立保留。"""
    body = response_body()
    body["answers"]["energy"]["confidence"] = confidence
    decision = jev.JevPresentationDecider._parse(body)
    assert decision.energy == pytest.approx(expected)
    assert decision.style_confidence == 0.88
    assert decision.gesture_confidence == 0.92
    assert decision.perform_probability == 0.95


@pytest.mark.parametrize(
    "none_probability,confidence,expected",
    [(0.11, 0.99, "none"), (0.1, 0.85, "wave"), (0.05, 0.849, "none")],
)
def test_action_posterior_and_confidence_preserve_both_gesture_gates(
    none_probability: float, confidence: float, expected: str
) -> None:
    """同一互斥分布产生动作概率，仍分别验证 0.90 概率门与 0.85 分类门，没有强制动作。"""
    body = response_body()
    gesture = body["answers"]["gesture"]
    gesture["probabilities"].update(none=none_probability, wave=1 - none_probability)
    gesture["confidence"] = confidence
    decision = jev.JevPresentationDecider._parse(body)
    assert decision.perform_probability == pytest.approx(1 - none_probability)
    assert choose_presentation(context(), decision).intent.gesture == expected


@pytest.mark.parametrize(
    "primitive", ["style", "gesture", "energy", "requested_gesture", "prohibited_gesture"]
)
def test_wrong_primitive_is_rejected(primitive: str) -> None:
    """各问题只能读取自己声明的原语，不接受字段相似的跨类型答案。"""
    body = response_body()
    body["answers"][primitive]["type"] = "unknown"
    with pytest.raises(ValueError):
        jev.JevPresentationDecider._parse_user(body)


@pytest.mark.parametrize(
    "answer,field",
    [
        ("style", "confidence"),
        ("gesture", "confidence"),
        ("requested_gesture", "confidence"),
        ("prohibited_gesture", "confidence"),
        ("energy", "confidence"),
    ],
)
@pytest.mark.parametrize("invalid", [True, "0.9", -0.1, 1.1, float("nan"), float("inf")])
def test_probabilities_and_confidence_require_finite_real_numbers(
    answer: str, field: str, invalid: Any
) -> None:
    """布尔值、字符串和非有限分数不能进入领域决策或动画强度。"""
    body = response_body()
    body["answers"][answer][field] = invalid
    with pytest.raises(ValueError):
        jev.JevPresentationDecider._parse_user(body)


@pytest.mark.parametrize(
    "answer", ["style", "gesture", "energy", "requested_gesture", "prohibited_gesture"]
)
@pytest.mark.parametrize("invalid", [True, -0.1, 1.1, "0.5", float("nan")])
def test_distribution_values_are_complete_finite_probabilities(answer: str, invalid: Any) -> None:
    """完整分类分布也独立验证数值，已选项合法不代表其他选项可以损坏。"""
    body = response_body()
    option = next(iter(body["answers"][answer]["probabilities"]))
    body["answers"][answer]["probabilities"][option] = invalid
    with pytest.raises(ValueError):
        jev.JevPresentationDecider._parse_user(body)


@pytest.mark.parametrize(
    "mutation",
    [
        "model",
        "unknown-style",
        "unknown-gesture",
        "missing-option",
        "extra-option",
        "sum",
        "wrong-choice",
        "score-range",
        "score-type",
        "weighted-score",
        "legend-key",
        "legend-value",
    ],
)
def test_model_enums_distributions_and_score_legend_are_consistent(mutation: str) -> None:
    """固定版本、全部枚举、最大概率选择及等级说明一致，异常响应必须整体拒绝。"""
    body = copy.deepcopy(response_body())
    answers = body["answers"]
    if mutation == "model":
        body["model"] = "jev-latest"
    elif mutation == "unknown-style":
        answers["style"]["choice"] = "angry"
    elif mutation == "unknown-gesture":
        answers["gesture"]["choice"] = "dance"
    elif mutation == "missing-option":
        answers["style"]["probabilities"].pop("shy")
    elif mutation == "extra-option":
        answers["gesture"]["probabilities"]["dance"] = 0
    elif mutation == "sum":
        answers["style"]["probabilities"]["neutral"] = 0.2
    elif mutation == "wrong-choice":
        answers["gesture"]["choice"] = "none"
    elif mutation == "score-range":
        answers["energy"]["score"] = 2.1
    elif mutation == "score-type":
        answers["energy"]["score"] = True
    elif mutation == "weighted-score":
        answers["energy"]["score"] = 1.2
    elif mutation == "legend-key":
        answers["energy"]["legend"]["3"] = "极端"
    else:
        answers["energy"]["legend"]["0"] = " "
    with pytest.raises(ValueError):
        jev.JevPresentationDecider._parse(body)


def test_openrouter_rounding_preserves_valid_decision_and_rejects_corruption() -> None:
    """接口两位小数会累计舍入；只允许对应误差预算，不能接受任意损坏的分布。"""
    body = response_body()
    body["answers"]["style"]["probabilities"].update(happy=0.89, neutral=0.1)
    body["answers"]["energy"].update(score=1.06, probabilities={"0": 0.0, "1": 0.95, "2": 0.05})
    decision = jev.JevPresentationDecider._parse(body)
    assert decision.intent.gesture == "wave"
    assert decision.energy == pytest.approx(0.53)
    body["answers"]["style"]["probabilities"]["happy"] = 0.8
    with pytest.raises(ValueError, match="分类分布"):
        jev.JevPresentationDecider._parse(body)


def test_observed_choice_disagreement_discards_only_affected_confidence() -> None:
    """真实近邻标签冲突不能称舍入合法，仅该答案归零置信度且不影响合法的独立证据。"""
    body = response_body()
    body["answers"]["style"].update(
        choice="curious",
        confidence=0.42,
        probabilities={
            name: 0.5
            if name == "neutral"
            else 0.49
            if name == "curious"
            else 0.01
            if name == "gentle"
            else 0
            for name in STYLE_LABELS
        },
    )
    body["answers"]["gesture"].update(
        choice="none",
        confidence=0.42,
        probabilities={
            name: 0.52
            if name == "none"
            else 0.25
            if name == "nod"
            else 0.23
            if name == "tilt"
            else 0
            for name in GESTURE_LABELS
        },
    )
    body["answers"]["energy"].update(
        score=0.99, confidence=0.98, probabilities={"0": 0.01, "1": 0.99, "2": 0}
    )
    user = jev.JevPresentationDecider._parse_user(body)
    assert user.presentation.intent.style == "curious"
    assert user.presentation.style_confidence == 0.0
    assert body["answers"]["style"]["confidence"] == 0.42
    assert user.presentation.intent.gesture == "none"
    assert choose_user_command(user) == choose_user_prohibition(user) == "none"
    cue = choose_presentation(context(), user.presentation)
    assert cue.intent.style == "neutral" and cue.intent.gesture == "none"
    assert cue.gesture_id == "" and cue.gesture_source == "none"
    body["answers"]["style"]["confidence"] = 1.0
    assert jev.JevPresentationDecider._parse_user(body).presentation.style_confidence == 0.0
    body["answers"]["requested_gesture"].update(
        choice="wave",
        confidence=0.99,
        probabilities={
            name: 0.99 if name == "wave" else 0.01 if name == "none" else 0
            for name in jev.COMMAND_LABELS
        },
    )
    assert choose_user_command(jev.JevPresentationDecider._parse_user(body)) == "wave"
    body["answers"]["style"]["probabilities"].update(curious=0.4899, gentle=0.0101)
    with pytest.raises(ValueError, match="分类分布"):
        jev.JevPresentationDecider._parse_user(body)


@pytest.mark.parametrize(
    "prohibited,expected", [("none", "nod"), ("wave", "nod"), ("nod", "none"), ("all", "none")]
)
def test_user_prohibition_precedes_command_but_reply_never_authorizes_it(
    prohibited: str, expected: str
) -> None:
    """指令只依据用户证据，只有同动作或全部禁止可以否决，回复自然动作保持独立。"""
    body = response_body()
    body["answers"]["requested_gesture"].update(
        choice="nod",
        confidence=0.94,
        probabilities={
            name: 0.96 if name == "nod" else 0.04 if name == "none" else 0
            for name in jev.COMMAND_LABELS
        },
    )
    body["answers"]["prohibited_gesture"].update(
        choice=prohibited,
        confidence=1.0,
        probabilities={name: float(name == prohibited) for name in jev.PROHIBITION_LABELS},
    )
    decision = jev.JevPresentationDecider._parse_user(body)
    assert decision.command == "nod"
    assert decision.command_confidence == 0.94
    assert decision.presentation.intent.gesture == "wave"
    assert choose_user_command(decision) == expected
    assert choose_user_prohibition(decision) == prohibited


@pytest.mark.asyncio
async def test_request_cache_belongs_to_exact_turn_and_user_text(http: Harness) -> None:
    """同一用户命令跨回复片段复用，修订文本或新轮次必须重新判断，缓存从不拥有播放身份。"""
    from dataclasses import replace

    decider = http.decider()
    for ctx in (
        context(),
        replace(context(), text="收到。", segment_id="b"),
        replace(context(), user_text="不要挥手"),
        replace(context(), turn_id="next-turn"),
    ):
        assert await decider.decide_user(ctx) is not None
    assert [call[1]["json"]["state"]["user"] for call in http.request_calls] == [
        "你好，挥挥手吧。",
        "不要挥手",
        "你好，挥挥手吧。",
    ]
    await decider.aclose()
    assert decider._user_result is None


@pytest.mark.asyncio
async def test_cancelled_caller_keeps_owned_request_until_session_close(http: Harness) -> None:
    """过期调用者不拥有用户判断，挂断必须回收共享请求且不保存残缺结果。"""
    wait = asyncio.Event()
    response = Response(wait=wait)
    http.request_responses.append(response)
    decider = http.decider()
    task = asyncio.create_task(decider.decide_user(context()))
    await response.content.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(http.calls) == 0 and decider._user_result is None
    assert response.exits == 0
    await decider.aclose()
    assert response.exits == 1 and decider._user_result is None


@pytest.mark.asyncio
async def test_cancelled_caller_reuses_pending_user_request_and_reply_is_independent(
    http: Harness,
) -> None:
    """重入用户端口复用精确请求，回复判断不等待命令网络且不重复计费。"""
    wait = asyncio.Event()
    user = Response(wait=wait)
    http.request_responses.append(user)
    http.responses.append(Response())
    decider = http.decider()
    old = asyncio.create_task(decider.decide_user(context("thinking")))
    await user.content.started.wait()
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old
    assert await decider.decide(context()) is not None
    assert not decider._user_task.done()
    fresh = asyncio.create_task(decider.decide_user(context()))
    wait.set()
    assert await fresh is not None
    assert len(http.request_calls) == 1 and len(http.calls) == 1
    await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"model": "typesafe/jev-1.13"}, [], "invalid-shape", 5])
async def test_incomplete_json_shape_degrades_only_presentation(http: Harness, body: Any) -> None:
    """缺失结构或错误解析版本只影响增强；不能因 HTTP 成功跳过响应契约。"""
    http.responses.append(Response(body=body))
    decider = http.decider()
    assert await decider.decide(context()) is None
    assert len(http.calls) == 1
    await decider.aclose()


@pytest.mark.asyncio
async def test_user_contract_contains_only_original_user_without_completion_history(
    http: Harness,
) -> None:
    """用户授权绝不接触 assistant，历史由领域解析重复，不给模型无关 none 事实干扰命令。"""
    from dataclasses import replace

    ctx = replace(context(), last_command="shake", text="我不摇头，去挥手。")
    decider = http.decider()
    assert await decider.decide_user(ctx) is not None
    request = http.request_calls[0][1]
    assert request["json"]["state"] == {"user": ctx.user_text}
    assert set(request["json"]["questions"]) == {
        "requested_gesture",
        "prohibited_gesture",
        "style",
        "gesture",
        "energy",
    }
    assert set(request["json"]["questions"]["requested_gesture"]["criteria"]) == set(
        jev.COMMAND_LABELS
    )
    assert set(request["json"]["questions"]["prohibited_gesture"]["criteria"]) == set(
        jev.PROHIBITION_LABELS
    )
    assert request["timeout"].total == PRESENTATION_TIMING.user_total
    assert request["proxy"] == "http://127.0.0.1:10808"
    assert request["allow_redirects"] is False
    await decider.aclose()


@pytest.mark.asyncio
async def test_concurrent_user_waiters_share_one_exact_input(http: Harness) -> None:
    """语义状态与指令入口可能同时到达，缓存锁必须避免重复付费且二者观察同一事实。"""
    response = Response(wait=asyncio.Event())
    http.request_responses.append(response)
    decider = http.decider()
    a = asyncio.create_task(decider.decide_user(context()))
    b = asyncio.create_task(decider.decide_user(context()))
    await response.content.started.wait()
    response.content.wait.set()
    first, second = await asyncio.gather(a, b)
    assert first is second and first is not None
    assert len(http.request_calls) == 1
    assert not decider._queries
    await decider.aclose()


@pytest.mark.asyncio
async def test_incremental_listening_keeps_short_network_budget(http: Harness) -> None:
    """HTTP 留给正式输入升级空间，倾听的短预算由覆盖握手和读取的外层唯一时钟保证。"""
    decider = http.decider()
    assert await decider.decide_user(context("listening")) is not None
    assert http.request_calls[0][1]["timeout"].total == PRESENTATION_TIMING.user_total
    await decider.aclose()


@pytest.mark.asyncio
async def test_new_user_identity_cancels_old_owned_request(http: Harness) -> None:
    """更改文本或轮次必须实际取消旧网络，迟到判断不能污染新缓存或恢复取消动作。"""
    from dataclasses import replace

    old = Response(wait=asyncio.Event())
    http.request_responses.extend([old, Response()])
    decider = http.decider()
    task = asyncio.create_task(decider.decide_user(context()))
    await old.content.started.wait()
    fresh = replace(context(), turn_id="fresh", user_text="别动")
    assert await decider.decide_user(fresh) is not None
    with pytest.raises(asyncio.CancelledError):
        await task
    assert old.exits == 1 and len(http.request_calls) == 2
    assert decider._user_identity == (fresh.turn_id, fresh.user_text)
    assert not decider._queries
    await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing", "wrong-version", "bad-command", "bad-prohibition", "auth"]
)
async def test_bad_user_result_cannot_enter_or_replay_cache(http: Harness, failure: str) -> None:
    """HTTP 成功并不代表授权合法；失败输入不自动重试，新身份仍可按熔断规则恢复。"""
    body = response_body()
    status = 200
    if failure == "missing":
        body["answers"].pop("requested_gesture")
    elif failure == "wrong-version":
        body["model"] = "jev-latest"
    elif failure == "bad-command":
        body["answers"]["requested_gesture"]["probabilities"]["dance"] = 0
    elif failure == "bad-prohibition":
        body["answers"]["prohibited_gesture"]["confidence"] = True
    else:
        status = 403
    http.request_responses.append(Response(status, body=body))
    decider = http.decider()
    assert await decider.decide_user(context()) is None
    assert await decider.decide_user(context()) is None
    assert decider._user_result is None
    assert len(http.request_calls) == 1
    await decider.aclose()


def test_recent_turn_is_bounded_and_never_overwrites_current_text() -> None:
    """历史只辅助当前含义；当前 user/assistant 保持独立字段，不拼接身份或控制头。"""
    from dataclasses import replace

    decider = jev.JevPresentationDecider(
        base_url="https://openrouter.ai", model="typesafe/jev-1.13", api_key="test"
    )
    ctx = replace(context(), recent_user_text="以前很伤心", recent_assistant_text="我在呢。")
    payload = decider._request(ctx)
    assert payload["state"]["user"] == ctx.user_text
    assert payload["state"]["assistant"] == ctx.text
    assert payload["state"]["previous_user_message"] == "以前很伤心"
    assert payload["state"]["previous_assistant_message"] == "我在呢。"
    assert set(payload["questions"]) == {"style", "gesture", "energy"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "connection", "timeout"])
async def test_failed_free_head_does_not_trip_post_circuit(http: Harness, failure: str) -> None:
    """三次官网失败后用户仍查询，回复再遇两故障仍可恢复；以行为证明预热不计入两通道。"""
    from dataclasses import replace

    for _ in range(3):
        response = Response(503 if failure == "status" else 200)
        if failure != "status":
            response.enter_error = (
                aiohttp.ClientConnectorError(None, OSError())
                if failure == "connection"
                else TimeoutError()
            )
        http.warm_responses.append(response)
    http.responses.extend([Response(500), Response(500), Response()])
    decider = http.decider()
    try:
        for _ in range(3):
            await decider.warmup()
        assert await decider.decide_user(replace(context(), phase="thinking")) is not None
        assert await decider.decide(context()) is None
        assert await decider.decide(context()) is None
        assert await decider.decide(context()) is not None
        assert len(http.warm_calls) == 3
        assert len(http.request_calls) == len(http.clients) == 1
        assert len(http.calls) == 3
        assert not decider._queries
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_slow_free_head_does_not_delay_user_or_reply_post(http: Harness) -> None:
    """官网 HEAD 未返回时两类正式判断仍立即发送，并与预热复用同一个会话连接池。"""
    warm = Response()
    warm.enter_wait = asyncio.Event()
    warm.enter_started = asyncio.Event()
    http.warm_responses.append(warm)
    http.responses.append(Response())
    decider = http.decider()
    warming = asyncio.create_task(decider.warmup())
    try:
        await warm.enter_started.wait()
        assert await asyncio.wait_for(decider.decide_user(context("thinking")), 0.1) is not None
        assert await asyncio.wait_for(decider.decide(context()), 0.1) is not None
        assert not warming.done() and not decider._warm_task.done()
        assert len(http.clients) == len(http.request_calls) == len(http.calls) == 1
        warm.enter_wait.set()
        await warming
    finally:
        await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["expired-reply", "expired-user", "closed", "canceled"])
async def test_expired_closed_or_canceled_permission_never_starts_paid_post(
    http: Harness, boundary: str
) -> None:
    """在实际 client.post 前验证许可，覆盖子任务未获调度即过期、取消或关闭的边界。"""
    decider = http.decider()
    try:
        assert decider._ensure_client()
        loop = asyncio.get_running_loop()
        deadline = loop.time() - 1 if boundary == "expired-reply" else loop.time() + 1
        if boundary == "expired-user":
            deadline = asyncio.timeout_at(loop.time() - 1)
        if boundary.startswith("expired"):
            with pytest.raises(TimeoutError):
                await decider._post(decider._user_request(context()), 8, deadline=deadline)
        elif boundary == "closed":
            await decider.aclose()
            assert await decider._post({}, 8, deadline=deadline) is None
        else:
            task = asyncio.create_task(decider._exchange({}, 8, deadline=deadline))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not http.calls and not http.request_calls and not decider._queries
    finally:
        await decider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before-post", "http"])
async def test_final_input_extends_pending_listening_without_second_post(
    http: Harness, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """在端口到达点同步收到完整输入，排除测试多一拍调度让短窗先过期，仍验证原预算升级。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        jev, "PRESENTATION_TIMING", SimpleNamespace(user_total=0.5, listening_total=0.03)
    )
    wait = asyncio.Event()
    response = Response(wait=wait if stage == "http" else None)
    started = response.content.started if stage == "http" else asyncio.Event()
    http.request_responses.append(response)
    decider = http.decider()
    loop = asyncio.get_running_loop()
    final = None
    original_started_set = started.set
    observed = {}

    def receive_final_at_boundary():
        """仅局部立即运行最终输入到首个等待点，保证升级先于下一拍截止回调而不修改真实时钟。"""
        nonlocal final
        original_started_set()
        timer = decider._user_timeout
        observed.update(timer=timer, before=timer.when(), received_at=loop.time())
        final = asyncio.eager_task_factory(loop, decider.decide_user(context("thinking")))

    monkeypatch.setattr(started, "set", receive_final_at_boundary)
    if stage == "before-post":
        original = decider._post

        async def held_post(payload, budget, *, deadline):
            """仅暂停调度，不替代许可守卫或网络，暴露旧短截止被错误冻结的回归。"""
            started.set()
            await wait.wait()
            return await original(payload, budget, deadline=deadline)

        monkeypatch.setattr(decider, "_post", held_post)
    try:
        partial = asyncio.create_task(decider.decide_user(context("listening")))
        await started.wait()
        assert final is not None and decider._user_complete
        assert observed["timer"] is decider._user_timeout
        assert observed["received_at"] < observed["before"] < observed["timer"].when()
        await asyncio.sleep(0.06)
        assert not partial.done() and not final.done()
        wait.set()
        a, b = await asyncio.gather(partial, final)
        assert a is b and a is not None
        assert len(http.request_calls) == 1 and not http.warm_calls
        assert not decider._queries
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_failed_listening_does_not_poison_final_but_final_is_not_replayed(
    http: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """增量超时只能在完整输入边界升级一次；最终输入失败或成功均不自动重放。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        jev, "PRESENTATION_TIMING", SimpleNamespace(user_total=0.2, listening_total=0.02)
    )
    old = Response(wait=asyncio.Event())
    http.request_responses.extend([old, Response()])
    decider = http.decider()
    try:
        assert await decider.decide_user(context("listening")) is None
        assert old.exits == 1 and len(http.request_calls) == 1
        result = await decider.decide_user(context("thinking"))
        assert result is not None
        assert await decider.decide_user(context("thinking")) is result
        assert len(http.request_calls) == 2 and not http.warm_calls
        assert not decider._queries
    finally:
        await decider.aclose()


@pytest.mark.asyncio
async def test_proactive_warmup_is_free_shared_and_owned(http: Harness) -> None:
    """舞台预热重复调用只握手一次；关闭必须回收它，不能因后台启动而泄漏连接。"""
    decider = http.decider()
    await asyncio.gather(decider.warmup(), decider.warmup())
    assert len(http.warm_calls) == 1 and not http.request_calls and not http.calls
    await decider.aclose()
    await decider.warmup()
    assert http.clients[0].closes == 1 and len(http.clients) == 1


@pytest.mark.asyncio
async def test_short_listening_deadlines_do_not_disable_final_commands(
    http: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三次轻回应过期后完整输入再遇两故障仍可恢复，以行为证明正常短窗不累计用户熔断。"""
    from dataclasses import replace
    from types import SimpleNamespace

    monkeypatch.setattr(
        jev, "PRESENTATION_TIMING", SimpleNamespace(user_total=0.5, listening_total=0.03)
    )
    http.request_responses.extend(Response(wait=asyncio.Event()) for _ in range(3))
    decider = http.decider()
    try:
        await decider.warmup()
        for index in range(3):
            assert (
                await decider.decide_user(
                    replace(context("listening"), user_text=f"稳定输入{index}")
                )
                is None
            )
        assert len(http.request_calls) == 3
        http.request_responses.extend([Response(500), Response(500), Response()])
        for index in range(2):
            assert (
                await decider.decide_user(replace(context("thinking"), turn_id=f"完整输入{index}"))
                is None
            )
        assert await decider.decide_user(context("thinking")) is not None
        assert len(http.request_calls) == 6 and not decider._queries
    finally:
        await decider.aclose()
