#!/usr/bin/env bash
# Agent 和 speech 共用一份受管源码清单；依赖仍分别由各自 uv.lock 管理。
set -euo pipefail
repository="${1:-/mnt/c/dev/rust/xiaoya}"
shift "$(( $# > 0 ? 1 : 0 ))"
uv_command="$(command -v uv || true)"
if [[ -z "$uv_command" ]]; then
  printf '%s\n' '未找到 uv，请先为当前 WSL 用户安装 uv。' >&2
  exit 1
fi
"$uv_command" run --no-project --python 3.12 \
  "$repository/deployment/sync_runtime.py" "$repository" "$@"
"$uv_command" sync --directory /opt/xiaoya/agent --locked --no-dev
"$uv_command" sync --directory /opt/xiaoya/speech --locked --no-dev --no-install-project
