"""验证 SDK 工具发现、命名空间、失败回收及真实本地 MCP 子进程。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents.llm import ToolError, function_tool

from xiaoya.infrastructure import mcp_tools
from xiaoya.infrastructure.mcp_tools import MCPTools


def config_file(tmp_path: Path, *, allowed: list[str] | None = None) -> str:
    """本地示例进程无需模型服务，让离线测试也覆盖真实 initialize/list/call 协议。"""
    item = {
        "id": "demo",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "xiaoya.interfaces.mcp_demo"],
        "timeout_seconds": 10,
    }
    if allowed is not None:
        item["allowed_tools"] = allowed
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [item]}), encoding="utf-8")
    return str(path)


async def test_real_stdio_mcp_discovers_and_calls_demo_tools(tmp_path: Path) -> None:
    """子进程只访问内置样例，确认工具前缀不改变发给 MCP 服务的原始工具名。"""
    runtime = MCPTools(config_file(tmp_path))
    try:
        tools = {tool.info.name: tool for tool in await asyncio.wait_for(runtime.tools(), 20)}
        assert set(tools) == {"demo__search_knowledge", "demo__get_demo_ticket"}
        knowledge = json.loads(await tools["demo__search_knowledge"]({"query": "便签"}))
        assert "本次通话" in knowledge["text"]
        ticket = json.loads(await tools["demo__get_demo_ticket"]({"ticket_id": "DEMO-001"}))
        assert '"demo": true' in ticket["text"]
        assert "已受理" in ticket["text"]
        missing = json.loads(await tools["demo__get_demo_ticket"]({"ticket_id": "UNKNOWN"}))
        assert '"found": false' in missing["text"]
    finally:
        await asyncio.wait_for(runtime.close(), 15)
    await runtime.close()
    with pytest.raises(RuntimeError, match="关闭"):
        await runtime.tools()


async def test_discovery_failure_closes_all_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使握手成功而工具发现失败，也必须释放连接并传播启动失败。"""
    server = Mock(
        initialized=False,
        initialize=AsyncMock(),
        list_tools=AsyncMock(side_effect=RuntimeError("hidden-secret")),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(mcp_tools, "_create_server", Mock(return_value=server))
    runtime = MCPTools(config_file(tmp_path))
    with pytest.raises(RuntimeError, match="demo") as error:
        await runtime.tools()
    assert "hidden-secret" not in str(error.value)
    server.aclose.assert_awaited_once()
    await runtime.close()
    server.aclose.assert_awaited_once()


@pytest.mark.parametrize("allowed", [[], ["missing_tool"]])
async def test_explicit_filters_do_not_fall_back_to_all_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allowed: list[str],
) -> None:
    """空白名单确实暴露零工具，拼错工具名必须报错，不能意外放开整个服务。"""
    server = Mock(
        initialized=False,
        initialize=AsyncMock(),
        list_tools=AsyncMock(return_value=[]),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(mcp_tools, "_create_server", Mock(return_value=server))
    runtime = MCPTools(config_file(tmp_path, allowed=allowed))
    try:
        if allowed:
            with pytest.raises(RuntimeError):
                await runtime.tools()
        else:
            assert await runtime.tools() == []
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "error", [TimeoutError(), RuntimeError("secret-token"), ToolError("secret-token")]
)
async def test_mcp_call_failure_is_not_retried_or_reported_as_success(error: Exception) -> None:
    """外部写操作结果可能未知，失败反馈隐藏原始异常并且不能自动重试。"""
    from xiaoya.infrastructure.mcp_settings import MCPSettings

    call = AsyncMock(side_effect=error)
    original = function_tool(
        call,
        raw_schema={
            "name": "save",
            "description": "测试写操作",
            "parameters": {"type": "object", "properties": {}},
        },
    )
    tool = mcp_tools._wrap_tool(MCPSettings(id="private", transport="stdio"), original)
    with pytest.raises(ToolError) as result:
        await tool({})
    assert "secret-token" not in str(result.value)
    assert "自动重试" in str(result.value)
    call.assert_awaited_once()


async def test_close_during_initialization_cancels_setup_and_releases_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job 在握手期间结束也要回收；关闭后不能继续发现工具或启动下一个进程。"""
    entered = asyncio.Event()

    async def initialize() -> None:
        """用受控等待模拟尚未完成的握手，取消由真实生命周期代码触发。"""
        entered.set()
        await asyncio.Event().wait()

    server = Mock(
        initialized=False, initialize=initialize, list_tools=AsyncMock(), aclose=AsyncMock()
    )
    monkeypatch.setattr(mcp_tools, "_create_server", Mock(return_value=server))
    runtime = MCPTools(config_file(tmp_path))
    initializing = asyncio.create_task(runtime.tools())
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(runtime.close(), 2)
    with pytest.raises(asyncio.CancelledError):
        await initializing
    server.list_tools.assert_not_awaited()
    server.aclose.assert_awaited_once()


async def test_later_server_failure_also_closes_previously_connected_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多服务接入须整体成功；第二个服务失败不能遗留第一个成功的连接。"""
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {"id": identifier, "transport": "stdio", "command": "python"}
                    for identifier in ("first", "second")
                ]
            }
        ),
        encoding="utf-8",
    )
    first = Mock(
        initialized=False,
        initialize=AsyncMock(),
        list_tools=AsyncMock(return_value=[]),
        aclose=AsyncMock(),
    )
    second = Mock(
        initialized=False, initialize=AsyncMock(side_effect=OSError("不可用")), aclose=AsyncMock()
    )
    monkeypatch.setattr(mcp_tools, "_create_server", Mock(side_effect=[first, second]))
    runtime = MCPTools(str(path))
    with pytest.raises(RuntimeError, match="second"):
        await runtime.tools()
    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()


async def test_mcp_deadline_cancels_a_stalled_call() -> None:
    """真实超时要结束等待而不是只识别服务抛出的 TimeoutError，避免语音会话永久思考。"""
    from xiaoya.infrastructure.mcp_settings import MCPSettings

    stopped = asyncio.Event()

    async def stalled(raw_arguments: dict) -> None:
        """受控阻塞只等待取消，finally 证明超时已经回收调用协程。"""
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    original = function_tool(
        stalled, raw_schema={"name": "lookup", "parameters": {"type": "object", "properties": {}}}
    )
    tool = mcp_tools._wrap_tool(
        MCPSettings(id="demo", transport="stdio", timeout_seconds=0.02), original
    )
    with pytest.raises(ToolError, match="超时"):
        await asyncio.wait_for(tool({}), 1)
    assert stopped.is_set()
