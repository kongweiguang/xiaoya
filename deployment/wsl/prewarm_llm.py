"""在 Agent 接单前检查显式配置的对话模型，不记录回复正文或凭据。"""

import asyncio

import httpx
from dotenv import load_dotenv

from xiaoya.infrastructure.settings import Settings


async def main() -> None:
    """本地模型先完成 CUDA 初始化，远程 DeepSeek 则先确认鉴权及非思考回复可用。"""
    load_dotenv("/opt/xiaoya/agent/.env.local", override=False)
    settings = Settings.from_environment()
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + (settings.llm_api_key or "not-required")},
            json={
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": "请只回复一个字：好"}],
                "max_tokens": 8,
                "stream": False,
                **settings.llm_extra_body,
            },
        )
        response.raise_for_status()
        choices = response.json().get("choices", [])
        if not choices or not choices[0].get("message", {}).get("content"):
            raise RuntimeError("对话模型启动检查没有返回有效正文")
    print("对话模型启动检查完成。")


if __name__ == "__main__":
    asyncio.run(main())
