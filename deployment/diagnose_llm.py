"""显式验证当前对话模型的 SSE 契约，不作为启动或部署的付费前置操作。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from xiaoya.infrastructure.settings import Settings


async def diagnose(environment: Path) -> None:
    """只统计有效正文与结束帧，不记录凭据、生成内容或把非流式响应算作通过。"""
    load_dotenv(environment, override=False)
    settings = Settings.from_environment()
    content = False
    finished = False
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST",
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + settings.llm_api_key},
            json={
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": "请只回复一个字：好"}],
                "max_tokens": 8,
                "stream": True,
                **settings.llm_extra_body,
            },
        ) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                raise RuntimeError("对话接口未返回 SSE")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    break
                chunk = json.loads(payload)
                content |= any(
                    choice.get("delta", {}).get("content") for choice in chunk.get("choices", [])
                )
    if not content or not finished:
        raise RuntimeError("对话接口缺少有效正文或 SSE 结束帧")
    print("对话模型 SSE 契约验证通过。")


def main() -> None:
    """单独执行才调用付费接口；固定 UTF-8，避免 Windows 控制台把中文结果误码。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env.local"))
    asyncio.run(diagnose(parser.parse_args().env))


if __name__ == "__main__":
    main()
