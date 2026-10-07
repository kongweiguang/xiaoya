"""支持 uv run python -m xiaoya 与安装后的命令共用同一入口。"""

from xiaoya.interfaces.cli import main

if __name__ == "__main__":
    main()
