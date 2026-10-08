"""MCP 配置独立于模型配置，私有凭据仅通过环境变量引用。"""

import json
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MCPSettings:
    """配置按会话装配；可能包含敏感参数的字段不进入日志表示。"""

    id: str
    transport: str
    timeout_seconds: float = 15
    url: str = field(default="", repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    command: str = field(default="", repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = field(default=None, repr=False)
    allowed_tools: tuple[str, ...] | None = None


def load_mcp_settings(
    filename: str, environ: Mapping[str, str] | None = None
) -> tuple[MCPSettings, ...]:
    """空路径表示未启用；显式配置错误必须阻止启动，不能静默丢失已声明的能力。"""
    if not filename:
        return ()
    source = os.environ if environ is None else environ
    path = Path(filename).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("VOICE_AGENT_MCP_CONFIG_FILE 无法读取或不是有效 JSON") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"servers"}
        or not isinstance(data["servers"], list)
    ):
        raise ValueError("MCP 配置必须是仅包含 servers 数组的对象")
    result = tuple(_parse_server(item, path.parent, source) for item in data["servers"])
    if len({item.id for item in result}) != len(result):
        raise ValueError("MCP 服务 id 不能重复")
    return result


def _environment_values(value: object, source: Mapping[str, str]) -> dict[str, str]:
    """只解析环境变量引用，不把缺失变量的值或凭据写入异常信息。"""
    if not isinstance(value, dict):
        raise ValueError("MCP 环境变量引用必须是对象")
    result = {}
    for key, variable in value.items():
        if not isinstance(key, str) or not key or not isinstance(variable, str) or not variable:
            raise ValueError("MCP 环境变量引用必须使用非空字符串")
        content = source.get(variable, "")
        if not content.strip():
            raise ValueError("MCP 引用的环境变量未设置")
        result[key] = content
    return result


def _parse_server(item: object, base: Path, source: Mapping[str, str]) -> MCPSettings:
    """协议、端口和鉴权位置提前校验，避免 SDK 猜测传输或从 URL 继承凭据。"""
    if not isinstance(item, dict):
        raise ValueError("MCP servers 条目必须是对象")
    common = {"id", "transport", "timeout_seconds", "allowed_tools"}
    transport = item.get("transport")
    specific = (
        {"command", "args", "env_vars", "cwd"} if transport == "stdio" else {"url", "headers_env"}
    )
    if set(item) - common - specific:
        raise ValueError("MCP 配置包含未知字段或混用了不同传输的字段")
    identifier = item.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,23}", identifier
    ):
        raise ValueError("MCP 服务 id 必须是字母开头的 1 至 24 位字母、数字、下划线或横线")
    if not isinstance(transport, str) or transport not in {"stdio", "streamable_http", "sse"}:
        raise ValueError("MCP transport 必须是 stdio/streamable_http/sse")
    timeout = item.get("timeout_seconds", 15)
    try:
        valid_timeout = type(timeout) in {int, float} and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("MCP timeout_seconds 必须是有限的正数")
    allowed = item.get("allowed_tools")
    if allowed is not None and (
        not isinstance(allowed, list)
        or any(not isinstance(name, str) or not name for name in allowed)
    ):
        raise ValueError("MCP allowed_tools 必须是工具名称数组")
    kwargs = {
        "id": identifier,
        "transport": transport,
        "timeout_seconds": float(timeout),
        "allowed_tools": tuple(allowed) if allowed is not None else None,
    }
    if transport == "stdio":
        command = item.get("command", "")
        args = item.get("args", [])
        cwd = item.get("cwd", ".")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("stdio MCP 必须配置 command")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("stdio MCP args 必须是字符串数组")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("stdio MCP cwd 必须是非空路径")
        return MCPSettings(
            **kwargs,
            command=sys.executable if command == "python" else command,
            args=tuple(args),
            cwd=str((base / cwd).resolve()),
            env=_environment_values(item.get("env_vars", {}), source),
        )
    address = item.get("url", "")
    try:
        url = urlsplit(address) if isinstance(address, str) else None
        valid = (
            url
            and url.scheme in {"http", "https"}
            and url.hostname
            and url.username is None
            and url.password is None
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("HTTP MCP url 必须是有效 HTTP/HTTPS 地址，鉴权使用 headers_env")
    return MCPSettings(
        **kwargs, url=address, headers=_environment_values(item.get("headers_env", {}), source)
    )
