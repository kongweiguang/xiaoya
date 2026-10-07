"""校验配置错误时保持离线并避免泄漏凭据。"""

from dataclasses import fields

import pytest

from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure.settings import Settings, load_agent_name, validate_credentials


def test_private_services_use_chinese_defaults(private_settings: Settings) -> None:
    """默认语言应贯穿识别与合成，业务文案仍由领域对象提供。"""
    settings = private_settings
    assert settings.language == "zh"
    assert settings.instructions == AssistantProfile().instructions
    assert settings.greeting == AssistantProfile().greeting


def test_environment_can_override_model_and_greeting(private_environment: dict[str, str]) -> None:
    """替换供应商参数不应要求改动领域用例，边界空白在配置层清理。"""
    settings = Settings.from_environment(
        private_environment
        | {"VOICE_AGENT_STT_MODEL": " local-whisper ", "VOICE_AGENT_GREETING": "欢迎"}
    )
    assert settings.stt_model == "local-whisper"
    assert settings.greeting == "欢迎"


@pytest.mark.parametrize(
    "field_name",
    [
        item.name
        for item in fields(Settings)
        if not item.name.endswith("_api_key") and item.name != "mcp_config_file"
    ],
)
def test_explicit_empty_configuration_fails(
    field_name: str, private_environment: dict[str, str]
) -> None:
    """必填项明确设置为空必须失败；外部工具路径的空值由独立测试覆盖。"""
    variable = f"VOICE_AGENT_{field_name.upper()}"
    with pytest.raises(ValueError, match=variable):
        Settings.from_environment(private_environment | {variable: "  "})


def test_missing_private_endpoints_never_get_public_defaults() -> None:
    """模型配置必须由部署者提供，不用 SDK 的公共默认值掩盖缺失配置。"""
    with pytest.raises(ValueError) as error:
        Settings.from_environment({})
    for service in ("STT", "LLM", "TTS"):
        assert f"VOICE_AGENT_{service}_BASE_URL" in str(error.value)


@pytest.mark.parametrize("service", ["STT", "LLM", "TTS"])
@pytest.mark.parametrize("url", ["wss://model.internal/v1", "http://", "http://[bad"])
def test_invalid_private_url_fails_before_sdk_construction(
    service: str, url: str, private_environment: dict[str, str]
) -> None:
    """非法 HTTP 地址应在客户端创建前失败，字段错误不包含密钥值。"""
    variable = f"VOICE_AGENT_{service}_BASE_URL"
    with pytest.raises(ValueError, match=variable):
        Settings.from_environment(private_environment | {variable: url})


def test_private_keys_are_optional_and_hidden_in_repr(private_environment: dict[str, str]) -> None:
    """无鉴权服务允许空密钥；有鉴权服务的密钥不得进入普通配置日志。"""
    assert Settings.from_environment(private_environment).stt_api_key == ""
    settings = Settings.from_environment(
        private_environment | {"VOICE_AGENT_STT_API_KEY": "hidden-private-key"}
    )
    assert settings.stt_api_key == "hidden-private-key"
    assert "hidden-private-key" not in repr(settings)


@pytest.mark.parametrize("key", ["", " ", "not-required"])
def test_deepseek_requires_its_own_key(private_environment: dict[str, str], key: str) -> None:
    """远程 DeepSeek 不能借用无鉴权占位值，错误也不能暴露环境中的其他凭据。"""
    with pytest.raises(ValueError, match="VOICE_AGENT_LLM_API_KEY"):
        Settings.from_environment(
            private_environment
            | {
                "VOICE_AGENT_LLM_BASE_URL": "https://api.deepseek.com",
                "VOICE_AGENT_LLM_MODEL": "deepseek-flash",
                "VOICE_AGENT_LLM_API_KEY": key,
            }
        )


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
def test_deepseek_disables_thinking_without_changing_private_speech(
    private_environment: dict[str, str], base_url: str
) -> None:
    """两种官方 API 前缀都关闭思考，独立语音端点与密钥不会随 LLM 切换。"""
    settings = Settings.from_environment(
        private_environment
        | {
            "VOICE_AGENT_LLM_BASE_URL": base_url,
            "VOICE_AGENT_LLM_MODEL": "deepseek-flash",
            "VOICE_AGENT_LLM_API_KEY": "deepseek-test-key",
        }
    )
    assert settings.llm_extra_body == {"thinking": {"type": "disabled"}}
    assert settings.stt_base_url == private_environment["VOICE_AGENT_STT_BASE_URL"]
    assert settings.tts_base_url == private_environment["VOICE_AGENT_TTS_BASE_URL"]
    assert settings.stt_api_key == settings.tts_api_key == ""
    assert "deepseek-test-key" not in repr(settings)
    assert Settings.from_environment(private_environment).llm_extra_body == {}


def test_agent_registration_does_not_need_model_configuration() -> None:
    """命令帮助与设备查询不是模型调用，应能在尚未配置服务时执行。"""
    assert load_agent_name({}) == "xiaoya"


def test_tts_response_format_must_be_decodable(private_environment: dict[str, str]) -> None:
    """只接受 SDK 能解码的音频格式，使部署错误在会话启动前可见。"""
    with pytest.raises(ValueError, match="VOICE_AGENT_TTS_RESPONSE_FORMAT"):
        Settings.from_environment(private_environment | {"VOICE_AGENT_TTS_RESPONSE_FORMAT": "raw"})


def test_missing_credentials_are_reported_together() -> None:
    """一次列出全部缺失项，减少用户逐项启动和排错。"""
    with pytest.raises(ValueError) as error:
        validate_credentials({})
    for variable in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        assert variable in str(error.value)


@pytest.mark.parametrize("url", ["https://example.com", "wss://", "wss://[bad", "not-a-url"])
def test_invalid_livekit_url_does_not_expose_secrets(url: str) -> None:
    """配置异常只指出字段，不将可能敏感的环境内容回显。"""
    with pytest.raises(ValueError, match="LIVEKIT_URL") as error:
        validate_credentials(
            {"LIVEKIT_URL": url, "LIVEKIT_API_KEY": "key", "LIVEKIT_API_SECRET": "hidden-secret"}
        )
    assert "hidden-secret" not in str(error.value)


def test_valid_credentials_are_accepted() -> None:
    """地址校验只检查协议结构，不以网络可用性代替配置合法性。"""
    validate_credentials(
        {
            "LIVEKIT_URL": "ws://livekit.internal:7880",
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": "secret",
        }
    )


def test_empty_mcp_path_explicitly_disables_external_tools(
    private_environment: dict[str, str],
) -> None:
    """MCP 是可选能力，空路径是有效关闭配置；模型服务的必填约束仍保持不变。"""
    assert (
        Settings.from_environment(
            private_environment | {"VOICE_AGENT_MCP_CONFIG_FILE": ""}
        ).mcp_config_file
        == ""
    )


def test_delivery_default_off_until_full_acceptance(private_environment):
    """缺省配置不能越过官方模型及真人验收门，显式预览仍可独立开启。"""
    environment = dict(private_environment)
    environment.pop("VOICE_AGENT_EXPRESSIVE_ENABLED", None)
    assert Settings.from_environment(environment).expressive_enabled is False
    environment["VOICE_AGENT_EXPRESSIVE_ENABLED"] = "true"
    assert Settings.from_environment(environment).expressive_enabled is True
