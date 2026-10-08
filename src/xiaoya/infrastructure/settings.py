"""环境配置只在外层读取，领域层不感知部署方式。"""

import os
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from urllib.parse import urlsplit

from xiaoya.domain.assistant import AssistantProfile


@dataclass(frozen=True, slots=True)
class Settings:
    """唯一模型链显式配置地址与独立密钥，不接纳已退役的供应商或本地服务不支持的选项。"""

    stt_base_url: str
    stt_model: str
    llm_base_url: str
    llm_model: str
    tts_base_url: str
    tts_model: str
    tts_voice: str
    stt_api_key: str = field(repr=False)
    llm_api_key: str = field(repr=False)
    tts_api_key: str = field(repr=False)
    agent_name: str = "xiaoya"
    language: str = "zh"
    tts_response_format: str = "wav"
    instructions: str = AssistantProfile().instructions
    greeting: str = AssistantProfile().greeting
    mcp_config_file: str = ""

    def __post_init__(self) -> None:
        """部署错误在客户端创建前失败，语言只接纳实际 WS 契约；官方密钥只发往 HTTPS。"""
        for service in ("stt", "tts"):
            _validate_url(
                getattr(self, f"{service}_base_url"),
                {"http", "https"},
                f"VOICE_AGENT_{service.upper()}_BASE_URL",
            )
            if not getattr(self, f"{service}_api_key").strip():
                raise ValueError(f"VOICE_AGENT_{service.upper()}_API_KEY 必须显式填写")
        _validate_url(self.llm_base_url, {"https"}, "VOICE_AGENT_LLM_BASE_URL")
        llm_url = urlsplit(self.llm_base_url)
        if (
            llm_url.hostname != "api.deepseek.com"
            or llm_url.port not in {None, 443}
            or llm_url.path not in {"", "/", "/v1", "/v1/"}
        ):
            raise ValueError("VOICE_AGENT_LLM_BASE_URL 仅支持官方 https://api.deepseek.com 或 /v1")
        if not self.llm_api_key.strip() or self.llm_api_key.strip() == "not-required":
            raise ValueError("VOICE_AGENT_LLM_API_KEY 必须填写 DeepSeek 的独立密钥")
        for service, model in (
            ("llm", "deepseek-flash"),
            ("stt", "paraformer-streaming"),
            ("tts", "cosyvoice3-0.5b"),
        ):
            if getattr(self, f"{service}_model") != model:
                raise ValueError(f"VOICE_AGENT_{service.upper()}_MODEL 仅支持 {model}")
        if self.tts_voice != "default":
            raise ValueError("VOICE_AGENT_TTS_VOICE 仅支持当前本地服务的 default 音色")
        if self.tts_response_format not in {"wav", "pcm"}:
            raise ValueError("VOICE_AGENT_TTS_RESPONSE_FORMAT 仅支持当前本地服务的 wav/pcm")
        if self.language not in {"zh", "en"}:
            raise ValueError("VOICE_AGENT_LANGUAGE 仅支持当前流式识别服务的 zh/en")

    @property
    def llm_extra_body(self) -> dict[str, object]:
        """唯一语音模型始终关闭深度思考，SDK 与显式诊断共用同一请求约束。"""
        return {"thinking": {"type": "disabled"}}

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """模型配置缺失必须失败；MCP 空路径明确表示不启用外部工具。"""
        source = os.environ if environ is None else environ
        values: dict[str, str] = {}
        missing: list[str] = []
        for item in fields(cls):
            variable = f"VOICE_AGENT_{item.name.upper()}"
            default = "" if item.default is MISSING else item.default
            value = source.get(variable, default).strip()
            if not value and item.name != "mcp_config_file":
                missing.append(variable)
            values[item.name] = value
        if missing:
            raise ValueError(
                f"缺少配置：{', '.join(missing)}。请在 .env.local 填写模型服务地址和参数。"
            )
        return cls(**values)


def load_agent_name(environ: Mapping[str, str] | None = None) -> str:
    """注册入口只需要派发名称，使帮助、设备列表和模型下载不依赖模型服务配置。"""
    source = os.environ if environ is None else environ
    name = source.get("VOICE_AGENT_AGENT_NAME", "xiaoya").strip()
    if not name:
        raise ValueError("VOICE_AGENT_AGENT_NAME 不能为空")
    return name


def _validate_url(value: str, schemes: set[str], variable: str) -> None:
    """禁止公共根域及子域和内嵌鉴权查询；密钥只走独立字段，异常仅报告字段名避免泄露。"""
    try:
        url = urlsplit(value)
        hostname = (url.hostname or "").rstrip(".")
        port = url.port
        valid_url = (
            url.scheme in schemes
            and bool(hostname)
            and (port is None or port > 0)
            and url.username is None
            and url.password is None
            and not url.fragment
            and "?" not in value
            and hostname != "openai.com"
            and not hostname.endswith(".openai.com")
            and hostname != "livekit.cloud"
            and not hostname.endswith(".livekit.cloud")
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError(f"{variable} 必须是有效的 {'/'.join(sorted(schemes))} 地址")


def validate_credentials(environ: Mapping[str, str] | None = None) -> None:
    """私有 LiveKit 仍需服务端签名凭据，终端本地会话不执行这项检查。"""
    source = os.environ if environ is None else environ
    required = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    missing = [name for name in required if not source.get(name, "").strip()]
    if missing:
        raise ValueError(
            f"缺少配置：{', '.join(missing)}。请复制 .env.example 为 .env.local 并填写凭据。"
        )
    _validate_url(source["LIVEKIT_URL"], {"ws", "wss"}, "LIVEKIT_URL")
