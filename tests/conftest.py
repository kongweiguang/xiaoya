"""共享私有服务的明确配置，测试不依赖开发者的环境变量或真实服务。"""

import pytest

from xiaoya.infrastructure.settings import Settings


@pytest.fixture
def private_environment() -> dict[str, str]:
    """不同地址使测试能发现三个模型意外共用网关或回退到默认地址。"""
    return {
        "VOICE_AGENT_STT_BASE_URL": "http://stt.internal:8001/v1",
        "VOICE_AGENT_STT_MODEL": "whisper-1",
        "VOICE_AGENT_LLM_BASE_URL": "http://llm.internal:8002/v1",
        "VOICE_AGENT_LLM_MODEL": "qwen3",
        "VOICE_AGENT_TTS_BASE_URL": "http://tts.internal:8003/v1",
        "VOICE_AGENT_TTS_MODEL": "tts-1",
        "VOICE_AGENT_TTS_VOICE": "private-voice",
        "VOICE_AGENT_EXPRESSIVE_ENABLED": "false",
    }


@pytest.fixture
def private_settings(private_environment: dict[str, str]) -> Settings:
    """通过生产配置解析路径创建设置，避免测试绕过必填项检查。"""
    return Settings.from_environment(private_environment)
