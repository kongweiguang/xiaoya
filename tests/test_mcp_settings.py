"""配置解析不连接网络，校验传输选择、环境变量引用与隐藏凭据。"""

import json
import sys
from pathlib import Path

import pytest

from xiaoya.infrastructure.mcp_settings import load_mcp_settings


def write_config(path: Path, servers: list[dict]) -> str:
    """集中生成临时 JSON，让测试输入经过真实文件解析而非直接构造配置对象。"""
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return str(path)


def test_stdio_configuration_uses_current_python_and_relative_cwd(tmp_path: Path) -> None:
    """Python 指向当前 uv 环境，工作目录按配置文件解析，不受启动终端目录影响。"""
    filename = write_config(
        tmp_path / "mcp.json",
        [
            {
                "id": "demo",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "example"],
                "cwd": "child",
                "env_vars": {"PRIVATE_TOKEN": "DEMO_TOKEN"},
                "allowed_tools": [],
            }
        ],
    )
    (config,) = load_mcp_settings(filename, {"DEMO_TOKEN": "secret-token"})
    assert config.command == sys.executable
    assert config.cwd == str(tmp_path / "child")
    assert config.env == {"PRIVATE_TOKEN": "secret-token"}
    assert config.allowed_tools == ()
    assert "secret-token" not in repr(config)


def test_http_credentials_are_explicit_environment_references(tmp_path: Path) -> None:
    """完整 Authorization 值从环境获取，默认模型或 OpenAI 密钥不能接管 MCP 鉴权。"""
    filename = write_config(
        tmp_path / "mcp.json",
        [
            {
                "id": "private",
                "transport": "streamable_http",
                "url": "http://mcp.internal/mcp",
                "headers_env": {"Authorization": "PRIVATE_MCP_AUTH"},
                "timeout_seconds": 20,
            }
        ],
    )
    (config,) = load_mcp_settings(filename, {"PRIVATE_MCP_AUTH": "Bearer hidden"})
    assert config.headers == {"Authorization": "Bearer hidden"}
    assert "hidden" not in repr(config)
    with pytest.raises(ValueError, match="环境变量"):
        load_mcp_settings(filename, {"OPENAI_API_KEY": "unrelated"})


@pytest.mark.parametrize(
    "change",
    [
        {"id": ""},
        {"id": "with space"},
        {"transport": "unknown"},
        {"transport": []},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"timeout_seconds": float("inf")},
        {"allowed_tools": "all"},
        {"allowed_tools": [1]},
        {"command": ""},
        {"args": "bad"},
        {"url": "http://wrong-field"},
        {"env_vars": {"TOKEN": "MISSING"}},
    ],
)
def test_invalid_mcp_entries_fail_without_echoing_values(tmp_path: Path, change: dict) -> None:
    """类型错误和跨协议字段提前拒绝，不让 SDK 猜测协议或把缺失能力静默忽略。"""
    item = {"id": "demo", "transport": "stdio", "command": "python"} | change
    with pytest.raises(ValueError, match="MCP"):
        load_mcp_settings(write_config(tmp_path / "mcp.json", [item]), {})


def test_duplicate_ids_and_broken_documents_are_rejected(tmp_path: Path) -> None:
    """服务标识决定工具名称，重复配置和截断文件不能产生含糊的调用目标。"""
    item = {"id": "demo", "transport": "stdio", "command": "python"}
    with pytest.raises(ValueError, match="重复"):
        load_mcp_settings(write_config(tmp_path / "mcp.json", [item, item]))
    path = tmp_path / "mcp.json"
    path.write_text('{"servers": [secret-data', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_mcp_settings(str(path))
    assert "secret-data" not in str(error.value)
    assert load_mcp_settings("") == ()


@pytest.mark.parametrize(
    "address",
    [
        "http://127.0.0.1:0/mcp",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:-1/mcp",
        "http://127.0.0.1:invalid-port/mcp",
        "http://private-user:private-secret@127.0.0.1/mcp",
        "http://:private-secret@127.0.0.1/mcp",
        "http://@127.0.0.1/mcp",
    ],
)
def test_http_mcp_rejects_invalid_ports_and_embedded_credentials(
    tmp_path: Path, address: str
) -> None:
    """空用户名仍是内嵌鉴权，所有非法地址在配置阶段拒绝且错误不回显凭据。"""
    filename = write_config(
        tmp_path / "mcp.json",
        [{"id": "private", "transport": "streamable_http", "url": address}],
    )
    with pytest.raises(ValueError, match="MCP url") as error:
        load_mcp_settings(filename, {})
    assert address not in str(error.value)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("port", [None, 1, 65535])
def test_http_mcp_accepts_implicit_and_boundary_ports(tmp_path: Path, port: int | None) -> None:
    """校验只拒绝非法端口，不为私有网关添加任意端口范围限制。"""
    address = "http://127.0.0.1" + (f":{port}" if port is not None else "") + "/mcp"
    filename = write_config(
        tmp_path / "mcp.json",
        [{"id": "private", "transport": "streamable_http", "url": address}],
    )
    assert load_mcp_settings(filename, {})[0].url == address
