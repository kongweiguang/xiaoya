"""共享当前唯一模型链的假配置，测试不读取开发者凭据或访问真实服务。"""

import pytest

from xiaoya.infrastructure.settings import Settings


@pytest.fixture
def private_environment() -> dict[str, str]:
    """语音使用独立模拟服务，DeepSeek 保留生产地址但仅用假密钥及内存传输验证协议。"""
    return {
        "VOICE_AGENT_STT_BASE_URL": "http://stt.internal:8001/v1",
        "VOICE_AGENT_STT_MODEL": "paraformer-streaming",
        "VOICE_AGENT_LLM_BASE_URL": "https://api.deepseek.com",
        "VOICE_AGENT_LLM_MODEL": "deepseek-flash",
        "VOICE_AGENT_TTS_BASE_URL": "http://tts.internal:8003/v1",
        "VOICE_AGENT_TTS_MODEL": "cosyvoice3-0.5b",
        "VOICE_AGENT_TTS_VOICE": "default",
        "VOICE_AGENT_STT_API_KEY": "not-required",
        "VOICE_AGENT_LLM_API_KEY": "deepseek-test-key",
        "VOICE_AGENT_TTS_API_KEY": "not-required",
    }


@pytest.fixture
def private_settings(private_environment: dict[str, str]) -> Settings:
    """通过生产配置解析路径创建设置，避免测试绕过必填项检查。"""
    return Settings.from_environment(private_environment)
