"""将唯一受管源码清单同步到 Linux 运行目录，密钥和模型留在各自生命周期内。"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

DIRECTORIES = (
    ("src/xiaoya", "agent/src/xiaoya"),
    ("services/speech/src/local_speech", "speech/src/local_speech"),
    ("examples/mcp-demo", "agent/examples/mcp-demo"),
)
FILES = (
    ("pyproject.toml", "agent/pyproject.toml"),
    ("uv.lock", "agent/uv.lock"),
    (".python-version", "agent/.python-version"),
    ("README.md", "agent/README.md"),
    ("mcp.example.json", "agent/mcp.example.json"),
    ("services/speech/pyproject.toml", "speech/pyproject.toml"),
    ("services/speech/uv.lock", "speech/uv.lock"),
    ("services/speech/.python-version", "speech/.python-version"),
    ("deployment/wsl/start-livekit.sh", "start-livekit.sh"),
    ("deployment/wsl/wait-speech.sh", "wait-speech.sh"),
)


def checked_target(runtime: Path, relative: str) -> Path:
    """固定清单内每级目录拒绝链接和非目录父级，删除目标必须是根内的专属路径。"""
    target = runtime / relative
    if not runtime.is_absolute():
        raise ValueError("运行目录必须是绝对路径")
    if runtime.resolve() != runtime.absolute() or target.resolve() != target.absolute():
        raise ValueError("运行目录及受管路径不得包含符号链接")
    target.relative_to(runtime)
    if target == runtime:
        raise ValueError("不能把运行根目录当作受管源码目录")
    for parent in (runtime, *target.parents):
        if parent == runtime or runtime in parent.parents:
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("受管路径不得包含链接")
            if parent.exists() and not parent.is_dir():
                raise ValueError("受管路径父级必须是目录")
    if target.is_symlink() or target.is_junction():
        raise ValueError("受管路径不得包含链接")
    return target


def directory_entries(source: Path, target: Path) -> tuple[list[Path], list[Path]]:
    """预检所有链接后才允许同步，后续目录异常不能造成前面目录已经被修改。"""
    if not source.is_dir():
        raise ValueError(f"缺少受管源码目录：{source.name}")
    source_entries = list(source.rglob("*"))
    target_entries = list(target.rglob("*")) if target.exists() else []
    entries = [source, target, *source_entries, *target_entries]
    if any(entry.is_symlink() or entry.is_junction() for entry in entries):
        raise ValueError("受管源码中不允许符号链接")
    if any(entry.exists() and not (entry.is_file() or entry.is_dir()) for entry in entries):
        raise ValueError("受管源码只允许普通文件和目录")
    return source_entries, target_entries


def sync_directory(source: Path, target: Path) -> dict[str, str]:
    """仅删除该专属目录中源端已经删除的文件，不用整运行目录的递归覆盖。"""
    source_entries, target_entries = directory_entries(source, target)
    files = {
        entry.relative_to(source): entry
        for entry in source_entries
        if entry.is_file() and "__pycache__" not in entry.parts and entry.suffix != ".pyc"
    }
    expected_directories = {
        parent for relative in files for parent in relative.parents if parent != Path(".")
    }
    # 类型变更先移除旧节点，随后创建父目录与复制；后置删除会阻塞 file↔directory 切换。
    for entry in sorted(target_entries, key=lambda item: len(item.parts), reverse=True):
        relative = entry.relative_to(target)
        if not (
            (entry.is_file() and relative in files)
            or (entry.is_dir() and relative in expected_directories)
        ):
            if entry.is_dir():
                entry.rmdir()
            else:
                entry.unlink()
    if target.is_file():
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for relative, entry in files.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, destination)
        hashes[relative.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
    return hashes


def sync_runtime(
    repository: Path, runtime: Path, *, configuration: bool = False, check_only: bool = False
) -> dict:
    """全局预检通过才执行写入；部署预检复用同一清单，私有配置仅由显式选项复制。"""
    directories = [
        (checked_target(repository, source), checked_target(runtime, target))
        for source, target in DIRECTORIES
    ]
    files = [
        (checked_target(repository, source), checked_target(runtime, target))
        for source, target in FILES
    ]
    if configuration:
        files.append(
            (checked_target(repository, ".env.local"), checked_target(runtime, "agent/.env.local"))
        )
        files.append(
            (
                checked_target(repository, ".tools/livekit.yaml"),
                checked_target(runtime, "livekit.yaml"),
            )
        )
        mcp = repository / "mcp.local.json"
        if mcp.exists() or mcp.is_symlink() or mcp.is_junction():
            files.append(
                (
                    checked_target(repository, "mcp.local.json"),
                    checked_target(runtime, "agent/mcp.local.json"),
                )
            )
    for source, _target in [*directories, *files]:
        if not source.exists() or source.is_symlink():
            raise ValueError(f"受管输入缺失或为链接：{source.name}")
    for source, target in directories:
        directory_entries(source, target)
    for source, target in files:
        if not source.is_file() or (target.exists() and not target.is_file()):
            raise ValueError(f"受管独立文件必须为普通文件：{source.name}")
    # uv 会写入这些运行环境，不能让链接把 Linux 安装引向 Windows 或其他项目。
    for relative in ("agent/.venv", "speech/.venv", "vendor/CosyVoice"):
        target = checked_target(runtime, relative)
        if target.exists() and not target.is_dir():
            raise ValueError("运行环境和模型依赖路径必须是目录")
    if check_only:
        return {}
    report = {
        target.relative_to(runtime).as_posix(): sync_directory(source, target)
        for source, target in directories
    }
    for source, target in files:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if target.name in {".env.local", "mcp.local.json", "livekit.yaml"}:
            target.chmod(0o600)
    return report


def main() -> None:
    """CLI 固定 /opt/xiaoya，避免参数错误把别的项目纳入源码清理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--configuration", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    report = sync_runtime(
        arguments.repository.resolve(strict=True),
        Path("/opt/xiaoya"),
        configuration=arguments.configuration,
        check_only=arguments.check,
    )
    print(json.dumps({"source_sha256": report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
