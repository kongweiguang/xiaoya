"""以独立进程验证 Windows 控制台错误不会变成停留中的失败会话。"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from xiaoya.interfaces import cli


def test_console_missing_configuration_fails_fast_with_utf8_output(tmp_path: Path) -> None:
    """模拟 GBK 重定向，缺失配置必须用可读中文退出，且不启动等待输入的 worker。"""
    environ = dict(os.environ)
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        environ[name] = ""
    for name in list(environ):
        if name.startswith("VOICE_AGENT_"):
            del environ[name]
    environ["PYTHONIOENCODING"] = "gbk"
    result = subprocess.run(
        [sys.executable, "-m", "xiaoya", "console", "--text"],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "缺少配置" in result.stderr
    assert "VOICE_AGENT_TTS_BASE_URL" in result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert "starting worker" not in result.stdout


@pytest.mark.parametrize("mode", ["console", "dev", "start"])
def test_official_cli_receives_entrypoint_and_preserves_exit_status(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """由官方工具托管进程，选项和解释器参数不能错位，子进程失败不能被报告为成功。"""
    validate = Mock()
    run = Mock(return_value=subprocess.CompletedProcess([], 7))
    monkeypatch.setattr(cli, "validate_configuration", validate)
    monkeypatch.setattr(cli.shutil, "which", Mock(return_value="lk.exe"))
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["xiaoya", mode, "--record", "--", "-X", "utf8"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 7
    validate.assert_called_once_with(require_livekit=mode != "console")
    run.assert_called_once_with(
        [
            "lk.exe",
            "agent",
            mode,
            "--record",
            str(Path(cli.__file__).resolve()),
            "--",
            "-X",
            "utf8",
        ],
        check=False,
    )
    assert os.environ["PYTHONIOENCODING"] == "utf-8"


def test_room_mode_rejects_missing_private_credentials_before_starting_cli(
    monkeypatch: pytest.MonkeyPatch, private_environment: dict[str, str]
) -> None:
    """完整模型配置不能掩盖缺失的房间凭据，以免 lk 从个人配置中选择默认云项目。"""
    for name, value in private_environment.items():
        monkeypatch.setenv(name, value)
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["xiaoya", "start"])
    with pytest.raises(SystemExit, match="LIVEKIT_URL"):
        cli.main()
    run.assert_not_called()


def test_console_help_works_without_private_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """帮助信息由官方 CLI 提供，部署者尚未填写私有服务时也能查看实际支持的参数。"""
    validate = Mock()
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(cli, "validate_configuration", validate)
    monkeypatch.setattr(cli.shutil, "which", Mock(return_value="lk.exe"))
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["xiaoya", "console", "--help"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    validate.assert_not_called()
    assert run.call_args.args[0] == ["lk.exe", "agent", "console", "--help"]


def test_official_sdk_discovers_module_level_server() -> None:
    """现代 runner 通过文件发现 AgentServer，入口迁移后仍须兼容包导入与 Windows spawn。"""
    from livekit.agents.cli.discover import get_import_data

    discovered = get_import_data(path=Path(cli.__file__))
    assert discovered.import_string == "xiaoya.interfaces.cli:server"
