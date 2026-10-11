"""TypeSafe 的批量判断适配器只处理文本，连接池与熔断预算按会话独立持有。"""

import json
import logging
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from xiaoya.domain.delivery import GESTURE_LABELS, STYLE_LABELS, DeliveryIntent
from xiaoya.domain.presentation import PresentationContext, PresentationDecision

_LOGGER = logging.getLogger(__name__)


class JevPresentationDecider:
    """直接异步 HTTP 避免 SDK 的隐藏重试；过期请求没有重放价值。"""

    def __init__(self, *, base_url: str, model: str, api_key: str) -> None:
        """延迟创建连接池使同步装配保持离线；凭据不进入 repr 或错误内容。"""
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._model = model
        self._key = api_key
        self._client: aiohttp.ClientSession | None = None
        self._disabled = False
        self._failures = 0
        self._cooldown = 0.0
        self._rate_backoff = 1.0
        self._closed = False

    async def decide(self, context: PresentationContext) -> PresentationDecision | None:
        """一批问题共享语义输入；认证错误停用，其他故障退避且绝不泄露响应正文。"""
        if self._closed or self._disabled or time.monotonic() < self._cooldown:
            return None
        if self._client is None:
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=0.9),
                headers={"Authorization": f"Bearer {self._key}"},
                raise_for_status=False,
            )
        started = time.monotonic()
        try:
            async with self._client.post(
                self._url,
                json=self._request(context),
                timeout=aiohttp.ClientTimeout(total=0.65 if context.phase == "listening" else 0.85),
                allow_redirects=False,
            ) as response:
                if response.status in {401, 403}:
                    self._disabled = True
                    _LOGGER.warning("Jev 鉴权失败，本次会话停用增强；请检查配置密钥")
                    return None
                if response.status in {429, 529}:
                    # 不重放旧输入；新输入也遵守指数退避和服务端更长的等待时间。
                    delay = max(
                        self._rate_backoff,
                        self._retry_after(response.headers.get("Retry-After")),
                    )
                    self._cooldown = time.monotonic() + delay
                    self._rate_backoff = min(delay * 2, 30.0)
                    return None
                if response.status != 200:
                    raise ValueError("Jev 状态异常")
                # 控制消息体限制保护网络内存，不限制用户输入或对话长度。
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(8192):
                    size += len(chunk)
                    if size > 65536:
                        raise ValueError("Jev 响应过大")
                    chunks.append(chunk)
                decision = self._parse(json.loads(b"".join(chunks)))
        except Exception as error:
            self._failure(type(error).__name__)
            return None
        self._failures = 0
        self._rate_backoff = 1.0
        _LOGGER.debug(
            "Jev 决策完成：耗时 %.1fms，模型 %s", (time.monotonic() - started) * 1000, self._model
        )
        return decision

    def _request(self, context: PresentationContext) -> dict[str, Any]:
        """英文标准配合原文中文，显式区分请求、引用、否定与假设，不推断未注册能力。"""
        return {
            "model": self._model,
            "state": {
                "phase": "speaking" if context.reply_preview else context.phase,
                "user": context.user_text,
                "assistant": context.text
                if context.reply_preview or context.phase == "speaking"
                else "",
                "voice_style": context.voice_style,
            },
            "questions": {
                "style": {
                    "type": "choice",
                    "instructions": (
                        "Choose a restrained, empathetic facial style appropriate to the actual "
                        "meaning. Sadness, risk and failure must not be celebrated. "
                        "Quotes and hypothetical events are not present events."
                    ),
                    "criteria": dict(STYLE_LABELS),
                },
                "gesture": {
                    "type": "choice",
                    "instructions": (
                        "Choose at most one supported gesture for the current spoken assistant "
                        "segment. Prefer none. wave only for greeting, goodbye, or an explicit "
                        "current request; nod for support; tilt for curiosity; shy for shyness; "
                        "shake for refusal. Negated, quoted, hypothetical and unsupported "
                        "action requests imply none."
                    ),
                    "criteria": dict(GESTURE_LABELS),
                },
                "perform": {
                    "type": "noul",
                    "instructions": (
                        "Does the currently speaking assistant actually commit to an appropriate "
                        "supported gesture now? Answer no while listening/thinking, for negations, "
                        "quotations, hypotheticals, unsupported actions, or empty assistant text."
                    ),
                },
                "energy": {
                    "type": "score",
                    "instructions": (
                        "Rate the restrained emotional intensity suitable for the current actual "
                        "meaning, rather than punctuation count."
                    ),
                    "criteria": ["Quiet, comforting", "Natural conversational", "Clearly excited"],
                },
            },
        }

    @staticmethod
    def _parse(body: Any) -> PresentationDecision:
        """各原语独立验证，Score 是等级加权值而非概率，不能与 Noul 混用。"""
        if body["model"] != "jev-1.13.0":
            raise ValueError("Jev 模型版本不匹配")
        answers = body["answers"]
        style, gesture, perform, energy = (
            answers[name] for name in ("style", "gesture", "perform", "energy")
        )
        if (style["type"], gesture["type"], perform["type"], energy["type"]) != (
            "choice",
            "choice",
            "noul",
            "score",
        ):
            raise ValueError("Jev 原语不匹配")
        score = energy["score"]
        if type(score) not in {float, int} or not math.isfinite(score) or not 0 <= score <= 2:
            raise ValueError("Jev 强度等级非法")
        for answer, options in (
            (style, STYLE_LABELS),
            (gesture, GESTURE_LABELS),
            (energy, {"0": "", "1": "", "2": ""}),
        ):
            probabilities = answer["probabilities"]
            if (
                set(probabilities) != set(options)
                or any(
                    type(value) not in {float, int}
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                    for value in probabilities.values()
                )
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.001)
            ):
                raise ValueError("Jev 分类分布非法")
        if not math.isclose(
            score,
            sum(int(level) * probability for level, probability in energy["probabilities"].items()),
            abs_tol=0.001,
        ):
            raise ValueError("Jev 强度与分布不一致")
        if set(energy["legend"]) != {"0", "1", "2"} or any(
            not isinstance(value, str) or not value.strip() for value in energy["legend"].values()
        ):
            raise ValueError("Jev 强度等级说明非法")
        for answer in (style, gesture):
            if answer["choice"] not in answer["probabilities"] or answer["probabilities"][
                answer["choice"]
            ] < max(answer["probabilities"].values()):
                raise ValueError("Jev 选择与分布不一致")
        confidence = energy["confidence"]
        if (
            type(confidence) not in {float, int}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Jev 强度置信度非法")
        return PresentationDecision(
            DeliveryIntent(style["choice"], gesture["choice"]),
            style["confidence"],
            gesture["confidence"],
            perform["noul"],
            score / 2 if confidence >= 0.65 else 0.5,
        )

    def _failure(self, category: str) -> None:
        """连续故障触发会话内熔断，日志只含类别而不携带网络异常原文。"""
        self._failures += 1
        if self._failures >= 3:
            self._cooldown = time.monotonic() + 30
            self._failures = 0
        _LOGGER.warning("Jev 增强已降级（%s）", category)

    @staticmethod
    def _retry_after(value: str | None) -> float:
        """同时支持 HTTP 秒数和日期；损坏头采用短退避，绝不立即重试同一请求。"""
        try:
            delay = float(value) if value else 1.0
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 1.0
        return max(1.0, delay) if math.isfinite(delay) else 1.0

    async def aclose(self) -> None:
        """回收幂等，关闭后不允许任何迟到事件再建立连接池。"""
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.close()
