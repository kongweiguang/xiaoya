"""MCP 独立入口不应借助 LiveKit CLI 或要求私有模型地址。"""

import subprocess
import sys
from unittest.mock import Mock

import pytest

from xiaoya.interfaces import cli


def test_mcp_command_is_forwarded_without_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP 可以单独启动以便排查协议，退出状态与真实服务结果保持一致。"""
    validate = Mock()
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(cli, "validate_configuration", validate)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["xiaoya", "mcp", "--transport", "streamable-http"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    validate.assert_not_called()
    run.assert_called_once_with(
        [sys.executable, "-m", "xiaoya.interfaces.mcp_demo", "--transport", "streamable-http"],
        check=False,
    )
