"""环境配置只在外层读取，领域层不感知部署方式。"""

import os
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from urllib.parse import urlsplit

from xiaoya.domain.assistant import AssistantProfile


@dataclass(frozen=True, slots=True)
class Settings:
    """模型服务必须显式配置，DeepSeek 与本地语音服务的密钥互相独立。"""

    stt_base_url: str
    stt_model: str
    llm_base_url: str
    llm_model: str
    tts_base_url: str
    tts_model: str
    tts_voice: str
    agent_name: str = "xiaoya"
    language: str = "zh"
    stt_protocol: str = "http"
    stt_api_key: str = field(default="", repr=False)
    llm_api_key: str = field(default="", repr=False)
    tts_api_key: str = field(default="", repr=False)
    tts_response_format: str = "wav"
    instructions: str = AssistantProfile().instructions
    greeting: str = AssistantProfile().greeting
    mcp_config_file: str = ""
    expressive_enabled: bool = False

    def __post_init__(self) -> None:
        """在创建客户端前校验地址，官方 DeepSeek 必须提供独立密钥以避免假成功。"""
        for name in ("stt_base_url", "llm_base_url", "tts_base_url"):
            _validate_url(getattr(self, name), {"http", "https"}, f"VOICE_AGENT_{name.upper()}")
        if urlsplit(self.llm_base_url).hostname == "api.deepseek.com" and (
            not self.llm_api_key.strip() or self.llm_api_key.strip() == "not-required"
        ):
            raise ValueError("VOICE_AGENT_LLM_API_KEY 必须填写 DeepSeek 的独立密钥")
        if self.stt_protocol not in {"http", "websocket"}:
            raise ValueError("VOICE_AGENT_STT_PROTOCOL 必须是 http/websocket")
        if self.tts_response_format not in {"mp3", "opus", "aac", "flac", "wav", "pcm"}:
            raise ValueError("VOICE_AGENT_TTS_RESPONSE_FORMAT 必须是 mp3/opus/aac/flac/wav/pcm")

    @property
    def llm_extra_body(self) -> dict[str, object]:
        """语音对话关闭 DeepSeek 默认深度思考，其他兼容服务不接收供应商专属字段。"""
        if urlsplit(self.llm_base_url).hostname == "api.deepseek.com":
            return {"thinking": {"type": "disabled"}}
        return {}

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """模型配置缺失必须失败；MCP 空路径明确表示不启用外部工具。"""
        source = os.environ if environ is None else environ
        values: dict[str, str | bool] = {}
        missing: list[str] = []
        for item in fields(cls):
            variable = f"VOICE_AGENT_{item.name.upper()}"
            default = "" if item.default is MISSING else item.default
            if isinstance(default, bool):
                flag = source.get(variable, str(default).lower()).strip().lower()
                if flag not in {"true", "false"}:
                    raise ValueError(f"{variable} 必须是 true/false")
                values[item.name] = flag == "true"
                continue
            value = source.get(variable, default).strip()
            if not value and not item.name.endswith("_api_key") and item.name != "mcp_config_file":
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
    """只报告字段名，避免地址中的凭据或查询参数进入异常日志。"""
    try:
        url = urlsplit(value)
        valid_url = url.scheme in schemes and bool(url.hostname)
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
