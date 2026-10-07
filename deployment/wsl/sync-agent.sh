#!/usr/bin/env bash
# 只同步本项目 Agent 代码和依赖；网页人物独立部署，进程生命周期由调用者管理。
set -euo pipefail
source_root="${1:-/mnt/c/dev/rust/xiaoya}"
runtime=/opt/xiaoya/agent
if [[ "$(readlink -f "$runtime")" != /opt/xiaoya/agent ]]; then
  printf '%s\n' '运行目录必须是 /opt/xiaoya/agent' >&2
  exit 1
fi
# 父目录也必须是实际目录，防止已存在的目录链接把同步或精确删除指向其他项目。
for relative in src/xiaoya src/xiaoya/infrastructure src/xiaoya/application src/xiaoya/domain; do
  if [[ "$(readlink -f "$runtime/$relative")" != "/opt/xiaoya/agent/$relative" ]]; then
    printf '%s\n' '源码目录必须位于 /opt/xiaoya/agent 内且不使用目录链接' >&2
    exit 1
  fi
done
# 先同步再精确移除已退休的模块，避免 cp -a 留下可被误装配的旧视频发布代码。
# 目标均为已验证运行目录下的明确路径，不使用宽泛匹配，也不触碰其他服务。
cp -a "$source_root/src/xiaoya/." "$runtime/src/xiaoya/"
rm -f -- "$runtime/src/xiaoya/infrastructure/animated_avatar.py" \
  "$runtime/src/xiaoya/application/present_avatar.py" \
  "$runtime/src/xiaoya/domain/avatar.py"
# 原始 PNG 保留在 Windows 源目录，已退出 Python 运行包；运行目录只清理该专属目录。
if [[ "$(readlink -f "$runtime/src/xiaoya/infrastructure/avatar_assets")" == \
  /opt/xiaoya/agent/src/xiaoya/infrastructure/avatar_assets ]]; then
  rm -rf -- /opt/xiaoya/agent/src/xiaoya/infrastructure/avatar_assets
fi
cp "$source_root/pyproject.toml" "$source_root/uv.lock" "$runtime/"
cp "$source_root/mcp.example.json" "$runtime/"
if [[ -f "$source_root/mcp.local.json" ]]; then
  cp "$source_root/mcp.local.json" "$runtime/"
fi
cd "$runtime"
uv_command="$(command -v uv || true)"
if [[ -z "$uv_command" ]]; then
  uv_command="$HOME/.local/bin/uv"
fi
if [[ ! -x "$uv_command" ]]; then
  printf '%s\n' '未找到 uv，请为当前 WSL 用户安装 uv。' >&2
  exit 1
fi
"$uv_command" sync --locked --no-dev
sha256sum src/xiaoya/bootstrap.py src/xiaoya/infrastructure/livekit_conversation.py
