#!/usr/bin/env bash
# systemd 的启动前置门只检查本项目 speech，避免 Agent 接单后才发现模型未完成预热。
set -euo pipefail
deadline=$((SECONDS + 300))
until curl --silent --fail --max-time 2 http://127.0.0.1:8001/health | \
  /opt/xiaoya/agent/.venv/bin/python -c '
import json
import sys
try:
    health = json.load(sys.stdin)
except (ValueError, TypeError):
    sys.exit(1)
expected = {"status": "ok", "stt": "paraformer-streaming", "tts": "cosyvoice3-0.5b", "voice": "default"}
sys.exit(0 if isinstance(health, dict) and all(health.get(k) == v for k, v in expected.items()) else 1)
'; do
  if (( SECONDS >= deadline )); then
    printf '%s\n' '本地语音模型预热超时，请检查 xiaoya-speech 日志。' >&2
    exit 1
  fi
  sleep 2
done
