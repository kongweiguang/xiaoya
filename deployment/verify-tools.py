"""使用显式配置的 LLM 验证五个内置工具；示例 MCP 仅按显式参数启用。"""

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession
from livekit.plugins import openai

from xiaoya.application.assistant_tools import AssistantTools
from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter, SystemClock
from xiaoya.infrastructure.mcp_tools import MCPTools
from xiaoya.infrastructure.settings import Settings


async def verify(*, demo: bool = False) -> dict:
    """只提交固定文案；模型选项与语音会话一致，验证工具执行后确实产生回复。"""
    settings = Settings.from_environment()
    model = openai.LLM(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        extra_body=settings.llm_extra_body,
    )
    runtime = MCPTools(
        str(Path(__file__).resolve().parents[1] / "mcp.example.json") if demo else ""
    )
    adapter = AssistantToolAdapter(AssistantTools(clock=SystemClock()))
    session = AgentSession(llm=model)
    cases = (
        ("current_time", "请调用时间工具，告诉我实际的北京时间和日期。"),
        ("calculate", "请用计算工具计算 (128*3+56)/4，告诉我结果。"),
        ("save_note", "请保存便签，标题是工具验证，内容是带耳机和充电器。"),
        ("list_notes", "请调用便签工具，列出本次通话保存的便签。"),
        ("delete_note", "请删除标题为工具验证的便签。"),
    )
    if demo:
        cases += (
            ("demo__search_knowledge", "请用知识库工具查询便签能否永久保存。"),
            ("demo__get_demo_ticket", "请用工单工具查询演示工单 DEMO-001。"),
        )
    results = []
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(runtime.close)
        cleanup.push_async_callback(model.aclose)
        cleanup.push_async_callback(session.aclose)
        tools = [*adapter.tools(), *await runtime.tools()]
        await session.start(
            agent=Agent(instructions=AssistantProfile().instructions, tools=tools), record=False
        )
        for expected, prompt in cases:
            offset = len(session.history.items)
            await asyncio.wait_for(session.run(user_input=prompt), timeout=45)
            items = session.history.items[offset:]
            names = [item.name for item in items if item.type == "function_call"]
            outputs = [item for item in items if item.type == "function_call_output"]
            replies = [
                item.text_content
                for item in items
                if item.type == "message" and item.role == "assistant" and item.text_content
            ]
            if expected not in names or any(item.is_error for item in outputs) or not replies:
                raise RuntimeError(f"工具 {expected} 未完成调用与回复闭环")
            results.append({"expected_tool": expected, "actual_tools": names, "reply": replies[-1]})
    return {
        "passed": True,
        "scope": "真实配置 LLM、五个内置工具；固定测试文字输入",
        "demo_mcp": demo,
        "livekit_room": False,
        "real_microphone": False,
        "results": results,
    }


def main() -> None:
    """固定 UTF-8 兼容 Windows 与 emoji；证据只含测试文案，不输出配置或外部异常正文。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="验证 LLM 的五种工具调用及后续回复")
    parser.add_argument("--demo", action="store_true", help="额外验证独立示例 MCP")
    parser.add_argument("--output", type=Path, default=Path(".tools/logs/tool-verification.json"))
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env.local", override=False)
    try:
        result = asyncio.run(verify(demo=args.demo))
    except Exception as error:
        raise SystemExit(
            f"工具验证失败（{type(error).__name__}），请核对模型的 function calling 支持。"
        ) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
