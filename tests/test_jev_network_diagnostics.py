"""连接错误仅增加安全结构信息，不把诊断日志变成网络重试或秘密输出。"""

import errno
import json
from types import SimpleNamespace

import aiohttp
import pytest
from test_jev import Response, context
from test_jev import http as http

from xiaoya.infrastructure.jev import JevPresentationDecider


@pytest.mark.parametrize("lane", ["user", "reply"])
@pytest.mark.parametrize("proxy_url", ["", "http://proxy-secret.invalid:10818"])
@pytest.mark.asyncio
async def test_connector_diagnostic_retains_errno_without_serializing_secrets(
    http, caplog, lane, proxy_url
) -> None:
    """真实请求失败路径保留底层类别；故意令异常字符串抛错，以防未来日志意外展开凭据。"""

    class RefuseString(aiohttp.ClientConnectorError):
        """异常可含代理与目标身份，因此测试禁止任何字符串化而非只查已知假密钥。"""

        def __str__(self) -> str:
            """一旦诊断试图格式化异常，测试须立刻失败而不能让脱敏断言漏掉新字段。"""
            raise AssertionError("connector error must not be formatted")

    error = RefuseString(
        SimpleNamespace(host="host-secret.invalid", port=443, ssl=True),
        OSError(errno.ECONNRESET, "body-secret user-audio-secret"),
    )
    response = Response()
    response.enter_error = error
    (http.request_responses if lane == "user" else http.responses).append(response)
    decider = JevPresentationDecider(
        base_url="https://host-secret.invalid/",
        model="typesafe/jev-1.13",
        api_key="key-secret",
        proxy_url=proxy_url,
    )
    try:
        result = (
            await decider.decide_user(context("thinking"))
            if lane == "user"
            else await decider.decide(context())
        )
        assert result is None
        records = [record for record in caplog.records if hasattr(record, "connector_errno")]
        assert len(records) == 1
        record = records[0]
        assert record.connector_errno == errno.ECONNRESET
        assert record.cause_type == "ConnectionResetError"
        assert record.is_proxy is bool(proxy_url)
        assert record.stage == "决策" and record.lane == lane
        serialized = json.dumps(record.__dict__, ensure_ascii=False, default=repr)
        assert all(
            secret not in serialized
            for secret in (
                "host-secret",
                "proxy-secret",
                "body-secret",
                "user-audio-secret",
                "key-secret",
            )
        )
        assert record.exc_info is None and record.stack_info is None
        assert decider._failures[lane] == 1
        assert len(http.request_calls) + len(http.calls) == 1
    finally:
        await decider.aclose()


@pytest.mark.parametrize("os_error", [OSError(), OSError(True, "secret-message")])
def test_connector_missing_or_invalid_errno_is_explicitly_unknown(caplog, os_error) -> None:
    """没有整数错误码就记录未知；布尔值不能因Python整数继承关系冒充有效errno。"""
    decider = JevPresentationDecider(
        base_url="https://example.invalid", model="test", api_key="test"
    )
    decider._query_failure(aiohttp.ClientConnectorError(None, os_error), lane="user")
    record = next(record for record in caplog.records if hasattr(record, "connector_errno"))
    assert record.connector_errno is None
    assert record.cause_type == type(os_error).__name__
    assert "secret-message" not in caplog.text


def test_other_failures_keep_original_category_without_connector_claim(caplog) -> None:
    """超时和解析失败没有已知连接错误码，不能为诊断方便把它们改称连接故障或打印正文。"""
    decider = JevPresentationDecider(
        base_url="https://example.invalid", model="test", api_key="test"
    )
    decider._query_failure(TimeoutError("timeout-secret"), lane="user")
    decider._query_failure(ValueError("payload-secret"), lane="reply")
    assert not any(hasattr(record, "connector_errno") for record in caplog.records)
    assert "timeout-secret" not in caplog.text and "payload-secret" not in caplog.text
    assert decider._failures == {"user": 1, "reply": 1}
