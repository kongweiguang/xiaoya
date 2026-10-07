"""通过官方 MCP 客户端发现工具，连接资源与一次 LiveKit Job 绑定。"""

import asyncio
import re
from contextlib import AsyncExitStack
from typing import Any

from livekit.agents.llm import RawFunctionTool, ToolError, function_tool
from livekit.agents.llm.mcp import MCPServer, MCPServerHTTP, MCPServerStdio, MCPToolset

from xiaoya.infrastructure.mcp_settings import MCPSettings, load_mcp_settings


class MCPTools:
    """显式预检所有连接，不让缺失工具的会话发送成功开场白。"""

    def __init__(self, config_file: str) -> None:
        """构造时不读文件或连接网络，阻塞配置读取留到线程中执行。"""
        self._config_file = config_file
        self._toolsets: list[MCPToolset] = []
        self._setup_task: asyncio.Task[list[RawFunctionTool]] | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def tools(self) -> list[RawFunctionTool]:
        """并发调用共享初始化；调用取消或初始化失败都先回收连接再传播错误。"""
        if self._close_task is not None:
            raise RuntimeError("MCP 工具连接已经关闭")
        if self._setup_task is None:
            self._setup_task = asyncio.create_task(self._load_tools())
        try:
            tools = await asyncio.shield(self._setup_task)
            if self._close_task is not None:
                raise RuntimeError("MCP 工具连接已经关闭")
            return tools.copy()
        except BaseException:
            try:
                await self.close()
            except Exception:
                pass  # 保留首个连接/取消错误，回收已尝试关闭所有服务。
            raise

    async def _load_tools(self) -> list[RawFunctionTool]:
        """握手和工具发现全部完成才发布结果，网络错误消息隐藏地址及鉴权信息。"""
        settings = await asyncio.to_thread(load_mcp_settings, self._config_file)
        tools = []
        try:
            for config in settings:
                toolset = MCPToolset(id=config.id, mcp_server=_create_server(config))
                self._toolsets.append(toolset)
                async with asyncio.timeout(config.timeout_seconds):
                    await toolset.setup()
                discovered = list(toolset.tools)
                names = {tool.info.name for tool in discovered}
                if config.allowed_tools is not None and set(config.allowed_tools) - names:
                    raise ValueError(f"MCP 服务 {config.id} 未提供 allowed_tools 中声明的工具")
                for tool in discovered:
                    if config.allowed_tools is None or tool.info.name in config.allowed_tools:
                        tools.append(_wrap_tool(config, tool))
            if len({tool.info.name for tool in tools}) != len(tools):
                raise ValueError("MCP 服务提供了重复工具名称")
        except Exception:
            raise RuntimeError(
                f"MCP 服务 {config.id} 初始化失败，请检查服务、工具清单和鉴权配置"
            ) from None
        return tools

    async def close(self) -> None:
        """回收由单个任务执行，取消等待者也不会遗留 MCP 子进程或连接。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_servers())
        await asyncio.shield(self._close_task)

    async def _close_servers(self) -> None:
        """先取消初始化防止关闭时新建连接；单项回收失败仍继续关闭其他服务。"""
        if self._setup_task is not None:
            if not self._setup_task.done():
                self._setup_task.cancel()
            await asyncio.gather(self._setup_task, return_exceptions=True)
        async with AsyncExitStack() as cleanup:
            for toolset in self._toolsets:
                cleanup.push_async_callback(toolset.aclose)
            self._toolsets.clear()


def _create_server(config: MCPSettings) -> MCPServer:
    """复用稳定 SDK 的协议实现，不启用已弃用的 Agent.mcp_servers 入口。"""
    if config.transport == "stdio":
        return MCPServerStdio(
            command=config.command,
            args=list(config.args),
            env=config.env,
            cwd=config.cwd,
            client_session_timeout_seconds=config.timeout_seconds,
        )
    return MCPServerHTTP(
        url=config.url,
        transport_type=config.transport,
        headers=config.headers,
        timeout=config.timeout_seconds,
        sse_read_timeout=config.timeout_seconds,
        client_session_timeout_seconds=config.timeout_seconds,
    )


def _wrap_tool(config: MCPSettings, tool: RawFunctionTool) -> RawFunctionTool:
    """对外名称加服务前缀，转发仍使用原工具；外部写操作失败不能自动重试。"""
    name = f"{config.id}__{tool.info.name}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError(f"MCP 服务 {config.id} 的工具名不符合兼容 LLM 的 64 字符规则")

    async def invoke(raw_arguments: dict[str, Any]) -> Any:
        """调用只执行一次；超时与传输异常表示结果未知，隐藏原始异常中的地址及凭据。"""
        try:
            async with asyncio.timeout(config.timeout_seconds):
                return await tool(raw_arguments)
        except TimeoutError:
            raise ToolError("MCP 工具调用超时，执行结果未确认。不要自动重试写操作。") from None
        except Exception:
            raise ToolError(
                "MCP 工具未返回可确认的结果，请检查服务。不要声称成功或自动重试写操作。"
            ) from None

    return function_tool(
        invoke,
        raw_schema={**tool.info.raw_schema, "name": name},
        on_duplicate="reject",
        duplicate_scope="name_and_args",
    )
