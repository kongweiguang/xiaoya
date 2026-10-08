"""通过官方 LiveKit CLI 启动，模块级 server 供现代 SDK 发现和 Windows 子进程导入。"""

import argparse
import os
import shutil
import subprocess
import sys
from io import TextIOWrapper
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import JobContext

from xiaoya.bootstrap import (
    agent_name,
    prepare_conversation,
    prepare_server,
    validate_configuration,
)

load_dotenv(Path.cwd() / ".env.local", override=False)

server = prepare_server()


@server.rtc_session(agent_name=agent_name())
async def voice_agent(context: JobContext) -> None:
    """协议入口只启动用例，持续收音与回复由 SDK 的 Job 生命周期托管。"""
    await prepare_conversation(context).execute()


def main() -> None:
    """本地语音与 DeepSeek 交给官方 CLI；独立示例不进入生产启动入口。"""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="LiveKit 中文语音助手")
    parser.add_argument("command", choices=("console", "dev", "start", "download-files"))
    parser.add_argument("options", nargs=argparse.REMAINDER, help="原样转发到所选启动命令的参数")
    arguments = parser.parse_args()
    if arguments.command == "download-files":
        command = [sys.executable, "-m", "livekit.agents", "download-files", *arguments.options]
    else:
        inspection_only = {"--help", "-h", "--list-devices"}.intersection(arguments.options)
        if not inspection_only:
            try:
                validate_configuration(require_livekit=arguments.command != "console")
            except ValueError as error:
                raise SystemExit(str(error)) from None
        executable = shutil.which("lk")
        local_executable = Path.cwd() / ".tools" / "lk.exe"
        if executable is None and local_executable.is_file():
            executable = str(local_executable)
        if executable is None:
            raise SystemExit(
                "缺少 LiveKit CLI。请运行 winget install LiveKit.LiveKitCLI，"
                "或将官方 Windows 二进制放到 .tools/lk.exe。"
            )
        options = arguments.options
        # lk 的参数位于入口路径前，分隔符后的参数属于 Python 解释器。
        separator = options.index("--") if "--" in options else len(options)
        entrypoint = [] if inspection_only else [str(Path(__file__).resolve())]
        command = [
            executable,
            "agent",
            arguments.command,
            *options[:separator],
            *entrypoint,
            *options[separator:],
        ]
    try:
        result = subprocess.run(command, check=False)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    raise SystemExit(result.returncode)
