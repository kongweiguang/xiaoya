"""用离线异步 HTTP 端口核对 TypeSafe 官方批量契约及每会话降级边界。"""

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
from xiaoya.domain.presentation import PresentationContext
from xiaoya.infrastructure import jev


def response_body() -> dict[str, Any]:
    """响应按真实原语构造，Noul 无置信度字段，Score 使用概率加权等级。"""
    return {
        "model": "jev-1.13.0",
        "answers": {
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
            "perform": {"type": "noul", "noul": 0.96},
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
        """返回端口自身，真实适配器持有并负责异步上下文退出。"""
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
        """一次调用只消费一个结果，隐式重试和错误重定向都会被断言发现。"""
        assert not self.closes
        self.harness.calls.append((url, kwargs))
        response = self.harness.responses.popleft()
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
    clients: list[Client] = field(default_factory=list)
    clock: Clock = field(default_factory=Clock)

    def client(self, **kwargs: Any) -> Client:
        """工厂保留每次分配，便于证明整个会话只创建一个连接池。"""
        instance = Client(self, kwargs)
        self.clients.append(instance)
        return instance

    def decider(self) -> jev.JevPresentationDecider:
        """只用明确假密钥，构造本身必须离线且不创建资源。"""
        return jev.JevPresentationDecider(
            base_url="https://api.typesafe.ai/", model="jev-1.13.0", api_key="jev-test-secret"
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
    """四种判断共享官方批量请求；复用客户端不将密钥或内部身份放入模型输入。"""
    decider = http.decider()
    assert http.clients == []
    http.responses.extend([Response(), Response()])
    first = await decider.decide(context())
    await decider.decide(context("listening"))
    assert first is not None
    assert first.intent.style == "happy"
    assert first.intent.gesture == "wave"
    assert first.energy == pytest.approx(0.525)
    assert first.perform_probability == pytest.approx(0.96)
    assert len(http.clients) == 1
    assert http.clients[0].options["headers"] == {"Authorization": "Bearer jev-test-secret"}
    assert http.clients[0].options["timeout"].total == 0.9
    url, request = http.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert request["allow_redirects"] is False
    assert request["timeout"].total == 0.85
    body = request["json"]
    assert body["model"] == "jev-1.13.0"
    assert body["state"]["user"] == "你好，挥挥手吧。"
    assert body["state"]["assistant"] == "你好呀，很高兴见到你。"
    assert body["state"]["voice_style"] == "neutral"
    assert {name: value["type"] for name, value in body["questions"].items()} == {
        "style": "choice",
        "gesture": "choice",
        "perform": "noul",
        "energy": "score",
    }
    assert set(body["questions"]["style"]["criteria"]) == set(STYLE_LABELS)
    assert set(body["questions"]["gesture"]["criteria"]) == set(GESTURE_LABELS)
    assert len(body["questions"]["energy"]["criteria"]) == 3
    assert "jev-test-secret" not in json.dumps(body)
    assert "user-turn" not in json.dumps(body)
    assert http.calls[1][1]["timeout"].total == 0.65
    assert http.calls[1][1]["json"]["state"]["assistant"] == ""
    await decider.aclose()
    await decider.aclose()
    assert http.clients[0].closes == 1
    assert await decider.decide(context()) is None
    assert len(http.calls) == 2


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
    """打断必须取消真实等待，CancelledError 不能被吞掉或增加服务失败计数。"""
    response = Response(wait=asyncio.Event())
    http.responses.extend([response, Response()])
    decider = http.decider()
    task = asyncio.create_task(decider.decide(context()))
    await response.content.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.exits == 1
    assert decider._failures == 0
    assert await decider.decide(context()) is not None
    assert len(http.clients) == 1
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
    assert await task is None
    await decider.aclose()
    assert await decider.decide(context()) is None
    assert http.clients[0].closes == 1
    assert response.exits == 1
    assert len(http.clients) == 1


@pytest.mark.parametrize("confidence,expected", [(0.649, 0.5), (0.65, 0.525)])
def test_score_is_weighted_level_and_noul_has_no_confidence(
    confidence: float, expected: float
) -> None:
    """强度等级按三级量表归一化，不能把 Score 原值或 Noul 当作 Choice 置信度。"""
    body = response_body()
    body["answers"]["energy"]["confidence"] = confidence
    decision = jev.JevPresentationDecider._parse(body)
    assert decision.energy == pytest.approx(expected)
    assert decision.style_confidence == 0.88
    assert decision.gesture_confidence == 0.92
    assert decision.perform_probability == 0.96


@pytest.mark.parametrize("primitive", ["style", "gesture", "perform", "energy"])
def test_wrong_primitive_is_rejected(primitive: str) -> None:
    """各问题只能读取自己声明的原语，不接受字段相似的跨类型答案。"""
    body = response_body()
    body["answers"][primitive]["type"] = "unknown"
    with pytest.raises(ValueError):
        jev.JevPresentationDecider._parse(body)


@pytest.mark.parametrize(
    "answer,field",
    [
        ("style", "confidence"),
        ("gesture", "confidence"),
        ("perform", "noul"),
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
        jev.JevPresentationDecider._parse(body)


@pytest.mark.parametrize("answer", ["style", "gesture", "energy"])
@pytest.mark.parametrize("invalid", [True, -0.1, 1.1, "0.5", float("nan")])
def test_distribution_values_are_complete_finite_probabilities(answer: str, invalid: Any) -> None:
    """完整分类分布也独立验证数值，已选项合法不代表其他选项可以损坏。"""
    body = response_body()
    option = next(iter(body["answers"][answer]["probabilities"]))
    body["answers"][answer]["probabilities"][option] = invalid
    with pytest.raises(ValueError):
        jev.JevPresentationDecider._parse(body)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"model": "jev-1.13.0"}, [], "invalid-shape", 5])
async def test_incomplete_json_shape_degrades_only_presentation(http: Harness, body: Any) -> None:
    """合法 JSON 仍可能缺失协议结构，错误只影响增强而不逃逸到语音链路。"""
    http.responses.append(Response(body=body))
    decider = http.decider()
    assert await decider.decide(context()) is None
    assert len(http.calls) == 1
    await decider.aclose()
