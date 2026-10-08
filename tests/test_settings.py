"""校验配置错误时保持离线并避免泄漏凭据。"""

from dataclasses import fields, replace

import pytest

from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure.settings import Settings, load_agent_name, validate_credentials


def test_private_services_use_chinese_defaults(private_settings: Settings) -> None:
    """默认语言应贯穿识别与合成，业务文案仍由领域对象提供。"""
    settings = private_settings
    assert settings.language == "zh"
    assert settings.instructions == AssistantProfile().instructions
    assert settings.greeting == AssistantProfile().greeting


@pytest.mark.parametrize("language", ["fr", "auto", "ZH", "zh-CN", "en-US"])
@pytest.mark.parametrize("source", ["environment", "direct"])
def test_language_rejects_values_unsupported_by_streaming_service(
    private_environment: dict[str, str], private_settings: Settings, language: str, source: str
) -> None:
    """配置和直接装配共用服务的精确语言契约，握手前失败且不回显输入或密钥。"""
    with pytest.raises(ValueError, match="VOICE_AGENT_LANGUAGE") as error:
        if source == "environment":
            Settings.from_environment(private_environment | {"VOICE_AGENT_LANGUAGE": language})
        else:
            replace(private_settings, language=language)
    assert language not in str(error.value)
    assert private_settings.llm_api_key not in str(error.value)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("source", ["environment", "direct"])
def test_language_accepts_both_current_streaming_languages(
    private_environment: dict[str, str], private_settings: Settings, language: str, source: str
) -> None:
    """固定语言白名单不能误删服务已有的英文能力，两种装配方式保持同一结果。"""
    settings = (
        Settings.from_environment(private_environment | {"VOICE_AGENT_LANGUAGE": language})
        if source == "environment"
        else replace(private_settings, language=language)
    )
    assert settings.language == language


@pytest.mark.parametrize("language", ["zh", "en"])
def test_environment_trims_language_without_aliases(
    private_environment: dict[str, str], language: str
) -> None:
    """环境边界仍允许无意义空白，但不引入大小写或地区代码的兼容映射。"""
    settings = Settings.from_environment(
        private_environment | {"VOICE_AGENT_LANGUAGE": f" {language} "}
    )
    assert settings.language == language


def test_environment_normalizes_explicit_models_and_overrides_greeting(
    private_environment: dict[str, str],
) -> None:
    """唯一模型不能切换为历史实现，但环境值边界空白和用户业务文案仍可正常处理。"""
    settings = Settings.from_environment(
        private_environment
        | {"VOICE_AGENT_STT_MODEL": " paraformer-streaming ", "VOICE_AGENT_GREETING": "欢迎"}
    )
    assert settings.stt_model == "paraformer-streaming"
    assert settings.greeting == "欢迎"


@pytest.mark.parametrize(
    "field_name",
    [item.name for item in fields(Settings) if item.name != "mcp_config_file"],
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
@pytest.mark.parametrize(
    "url",
    [
        "wss://model.internal/v1",
        "http://",
        "http://[bad",
        "http://private:invalid",
        "http://private:99999",
        "http://private:0",
        "http://hidden:key@private/v1",
        "https://api.openai.com/v1",
        "https://api.openai.com./v1",
        "https://tenant.livekit.cloud/v1",
    ],
)
def test_invalid_private_url_fails_before_sdk_construction(
    service: str, url: str, private_environment: dict[str, str]
) -> None:
    """非法 HTTP 地址应在客户端创建前失败，字段错误不包含密钥值。"""
    variable = f"VOICE_AGENT_{service}_BASE_URL"
    with pytest.raises(ValueError, match=variable):
        Settings.from_environment(private_environment | {variable: url})


@pytest.mark.parametrize("hostname", ["openai.com", "OPENAI.COM", "openai.com.", "OPENAI.COM."])
@pytest.mark.parametrize("service", ["STT", "TTS"])
@pytest.mark.parametrize("source", ["environment", "direct"])
def test_speech_rejects_public_openai_root_before_clients_receive_keys(
    private_environment: dict[str, str],
    private_settings: Settings,
    hostname: str,
    service: str,
    source: str,
) -> None:
    """根域与子域共享公共端点禁令，两种装配入口及 DNS 等价写法都不能向其发送私有密钥。"""
    variable = f"VOICE_AGENT_{service}_BASE_URL"
    url = f"https://{hostname}/v1"
    with pytest.raises(ValueError, match=variable) as error:
        if source == "environment":
            Settings.from_environment(private_environment | {variable: url})
        else:
            replace(private_settings, **{f"{service.lower()}_base_url": url})
    assert url not in str(error.value)
    assert private_settings.llm_api_key not in str(error.value)


def test_private_keys_are_explicit_and_hidden_in_repr(private_environment: dict[str, str]) -> None:
    """无鉴权也必须显式占位，不能从其他供应商环境继承鉴权；密钥不进入配置日志。"""
    assert Settings.from_environment(private_environment).stt_api_key == "not-required"
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


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1/",
        "https://api.deepseek.com:443",
    ],
)
def test_deepseek_disables_thinking_without_changing_private_speech(
    private_environment: dict[str, str], base_url: str
) -> None:
    """官方前缀和标准 HTTPS 端口始终关闭思考，私有语音密钥也不会被 LLM 配置接管。"""
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
    assert settings.stt_api_key == settings.tts_api_key == "not-required"
    assert "deepseek-test-key" not in repr(settings)
    assert Settings.from_environment(private_environment).llm_extra_body == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com",
        "http://old-ollama.internal/v1",
        "https://llm.internal/v1",
        "https://api.deepseek.com.attacker.invalid",
        "https://sub.api.deepseek.com",
        "https://api.deepseek.com.",
        "https://api.deepseek.com:80",
        "https://api.deepseek.com:8443",
        "https://api.deepseek.com/anthropic",
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com/v2",
        "https://api.deepseek.com?key=hidden-key",
        "https://api.deepseek.com/v1?",
        "https://api.deepseek.com/v1#hidden-key",
        "https://hidden-key@api.deepseek.com",
    ],
)
def test_llm_rejects_nonstandard_or_retired_endpoints(
    private_environment: dict[str, str], base_url: str
) -> None:
    """固定官方 HTTPS 边界防止明文密钥、历史模型入口或拼接错误，异常不能复制敏感地址。"""
    with pytest.raises(ValueError, match="VOICE_AGENT_LLM_BASE_URL") as error:
        Settings.from_environment(private_environment | {"VOICE_AGENT_LLM_BASE_URL": base_url})
    assert "hidden-key" not in str(error.value)


@pytest.mark.parametrize(
    "service,model",
    [
        ("LLM", "qwen3"),
        ("LLM", "deepseek-chat"),
        ("LLM", "deepseek-reasoner"),
        ("STT", "local-whisper"),
        ("STT", "paraformer"),
        ("TTS", "tts-1"),
        ("TTS", "cosyvoice2"),
    ],
)
def test_models_match_current_single_pipeline(
    private_environment: dict[str, str], service: str, model: str
) -> None:
    """当前固定模型只接纳已验收协议，不能接受配置后等网络调用反复失败才发现退役选项。"""
    variable = f"VOICE_AGENT_{service}_MODEL"
    with pytest.raises(ValueError, match=variable):
        Settings.from_environment(private_environment | {variable: model})


def test_local_tts_rejects_voice_not_registered_by_service(
    private_environment: dict[str, str],
) -> None:
    """三种语气使用同一实际音色，SDK 可支持的名字不代表当前 CosyVoice 服务已经登记。"""
    with pytest.raises(ValueError, match="VOICE_AGENT_TTS_VOICE"):
        Settings.from_environment(private_environment | {"VOICE_AGENT_TTS_VOICE": "alloy"})


def test_deepseek_key_cannot_be_inherited_from_public_environment(
    private_environment: dict[str, str],
) -> None:
    """即使环境有公共协议密钥，独立必填项缺失也必须离线失败而不把它发送给 DeepSeek。"""
    environment = dict(private_environment)
    del environment["VOICE_AGENT_LLM_API_KEY"]
    environment["OPENAI_API_KEY"] = "must-not-be-inherited"
    with pytest.raises(ValueError, match="VOICE_AGENT_LLM_API_KEY") as error:
        Settings.from_environment(environment)
    assert "must-not-be-inherited" not in str(error.value)


def test_agent_registration_does_not_need_model_configuration() -> None:
    """命令帮助与设备查询不是模型调用，应能在尚未配置服务时执行。"""
    assert load_agent_name({}) == "xiaoya"


@pytest.mark.parametrize("format_name", ["mp3", "opus", "aac", "flac", "raw", "WAV"])
def test_tts_response_format_must_match_local_service(
    private_environment: dict[str, str], format_name: str
) -> None:
    """本地服务只输出 PCM/WAV，SDK 解码能力不能代替实际服务契约或触发额外格式回退。"""
    with pytest.raises(ValueError, match="VOICE_AGENT_TTS_RESPONSE_FORMAT"):
        Settings.from_environment(
            private_environment | {"VOICE_AGENT_TTS_RESPONSE_FORMAT": format_name}
        )


@pytest.mark.parametrize("format_name", ["wav", "pcm"])
def test_tts_accepts_both_current_audio_formats(
    private_environment: dict[str, str], format_name: str
) -> None:
    """唯一 PCM 链路的两种实际输出可显式选择，不把薄 WAV 包装误当作历史实现删除。"""
    assert (
        Settings.from_environment(
            private_environment | {"VOICE_AGENT_TTS_RESPONSE_FORMAT": format_name}
        ).tts_response_format
        == format_name
    )


def test_missing_credentials_are_reported_together() -> None:
    """一次列出全部缺失项，减少用户逐项启动和排错。"""
    with pytest.raises(ValueError) as error:
        validate_credentials({})
    for variable in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        assert variable in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "wss://",
        "wss://[bad",
        "not-a-url",
        "wss://livekit.internal?access_token=hidden-key",
        "ws://livekit.internal?",
    ],
)
def test_invalid_livekit_url_does_not_expose_secrets(url: str) -> None:
    """配置异常只指出字段，不将可能敏感的环境内容回显。"""
    with pytest.raises(ValueError, match="LIVEKIT_URL") as error:
        validate_credentials(
            {"LIVEKIT_URL": url, "LIVEKIT_API_KEY": "key", "LIVEKIT_API_SECRET": "hidden-secret"}
        )
    assert "hidden-secret" not in str(error.value)
    assert "hidden-key" not in str(error.value)


@pytest.mark.parametrize("hostname", ["openai.com", "OPENAI.COM", "openai.com.", "OPENAI.COM."])
@pytest.mark.parametrize("scheme", ["ws", "wss"])
def test_livekit_rejects_public_openai_root_without_exposing_credentials(
    hostname: str, scheme: str
) -> None:
    """房间入口复用同一禁止域边界，协议变化不能绕过校验或把签名凭据带入错误信息。"""
    url = f"{scheme}://{hostname}"
    with pytest.raises(ValueError, match="LIVEKIT_URL") as error:
        validate_credentials(
            {
                "LIVEKIT_URL": url,
                "LIVEKIT_API_KEY": "hidden-key",
                "LIVEKIT_API_SECRET": "hidden-secret",
            }
        )
    assert url not in str(error.value)
    assert "hidden-key" not in str(error.value)
    assert "hidden-secret" not in str(error.value)


@pytest.mark.parametrize("service", ["STT", "TTS"])
def test_speech_base_url_rejects_inline_query_credentials(private_environment, service) -> None:
    """语音鉴权必须走独立字段，基础 URL 查询不参与协议拼接，也不能进入 SDK 请求日志。"""
    variable = f"VOICE_AGENT_{service}_BASE_URL"
    with pytest.raises(ValueError, match=variable) as error:
        Settings.from_environment(
            private_environment | {variable: "http://speech.internal/v1?api_key=hidden-key"}
        )
    assert "hidden-key" not in str(error.value)


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
