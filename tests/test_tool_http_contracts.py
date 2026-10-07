"""通过真实 SDK 与内存传输验证 LLM 工具闭环及 MCP Streamable HTTP 协议。"""

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
from livekit.agents import Agent, AgentSession
from livekit.plugins import openai
from openai import AsyncOpenAI

from xiaoya.application.assistant_tools import AssistantTools
from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter
from xiaoya.infrastructure.mcp_tools import MCPTools


def sse_response(deltas: list[dict], finish_reason: str) -> httpx.Response:
    """模拟分片参数而非完整一次性 JSON，覆盖模型 SSE 解码与工具调用组装。"""
    chunks = []
    for delta in [*deltas, {}]:
        chunk = {
            "id": "private-tools",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "private-model",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason if not delta else None}
            ],
        }
        chunks.append(f"data: {json.dumps(chunk)}\n\n")
    return httpx.Response(
        200,
        text="".join(chunks) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


async def test_private_llm_executes_tool_and_resumes_reply() -> None:
    """真实 AgentSession 执行工具并回传 tool 消息，避免接入仅停留在请求含 tools 字段。"""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        """第二轮必须拿到第一轮计算结果，错误路由暴露意外公共 API 或缺失闭环。"""
        assert request.url.host == "llm.internal"
        assert request.headers["authorization"] == "Bearer private-key"
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse_response(
                [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-calculate",
                                "type": "function",
                                "function": {"name": "calculate", "arguments": ""},
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"expression":"(128*3'}}
                        ]
                    },
                    {"tool_calls": [{"index": 0, "function": {"arguments": '+56)/4"}'}}]},
                ],
                "tool_calls",
            )
        (tool_message,) = [item for item in body["messages"] if item["role"] == "tool"]
        assert tool_message["tool_call_id"] == "call-calculate"
        assert ast.literal_eval(tool_message["content"])["result"] == "110"
        return sse_response([{"content": "计算结果是110。"}], "stop")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
    model = openai.LLM(
        model="private-model",
        base_url="http://llm.internal/v1",
        api_key="private-key",
        client=AsyncOpenAI(
            base_url="http://llm.internal/v1", api_key="private-key", http_client=client
        ),
    )
    session = AgentSession(llm=model)
    adapter = AssistantToolAdapter(AssistantTools(clock=Mock()))
    try:
        await session.start(
            agent=Agent(instructions=AssistantProfile().instructions, tools=adapter.tools()),
            record=False,
        )
        await asyncio.wait_for(session.run(user_input="帮我算一下 (128*3+56)/4"), 10)
        assert len(requests) == 2
        assert {item["function"]["name"] for item in requests[0]["tools"]} == {
            "current_time",
            "calculate",
            "save_note",
            "list_notes",
            "delete_note",
        }
        assert any(
            item.type == "message"
            and item.role == "assistant"
            and item.text_content == "计算结果是110。"
            for item in session.history.items
        )
    finally:
        await session.aclose()
        await model.aclose()
        await client.aclose()


async def test_streamable_http_mcp_wire_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """内存 HTTP 覆盖真实 MCP 握手、通知、发现和调用，不依赖外部服务器。"""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        """兼容静态无会话服务器，记录线上参数确保前缀仅用于 LLM，不发给原 MCP 工具。"""
        assert request.url == "http://mcp.internal/mcp"
        assert request.headers["authorization"] == "Bearer mcp-key"
        assert request.method == "POST"
        body = json.loads(request.content)
        requests.append(body)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "private-demo", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "查询演示业务信息",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                        },
                    }
                ]
            }
        else:
            assert method == "tools/call"
            assert body["params"]["name"] == "lookup"
            assert body["params"]["arguments"] == {"id": "DEMO-001"}
            result = {"content": [{"type": "text", "text": "已受理（演示数据）"}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original = httpx.AsyncClient

    class MemoryClient(original):
        """保留 SDK 的 HTTP 构造与协议逻辑，替换传输并禁用宿主机代理。"""

        def __init__(self, *args, **kwargs) -> None:
            """所有 MCP 请求都留在内存，测试环境变量不能引入外部网络调用。"""
            kwargs.update(transport=httpx.MockTransport(respond), trust_env=False)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MemoryClient)
    monkeypatch.setenv("DEMO_MCP_AUTH", "Bearer mcp-key")
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "business",
                        "transport": "streamable_http",
                        "url": "http://mcp.internal/mcp",
                        "headers_env": {"Authorization": "DEMO_MCP_AUTH"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime = MCPTools(str(path))
    try:
        (tool,) = await asyncio.wait_for(runtime.tools(), 5)
        assert tool.info.name == "business__lookup"
        assert "已受理" in await tool({"id": "DEMO-001"})
    finally:
        await runtime.close()
    assert [item["method"] for item in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
