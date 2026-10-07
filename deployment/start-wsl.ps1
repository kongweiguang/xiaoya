# systemd 服务本身不会保持 WSL 存活；以真实 Agent 前台进程维持本机后端运行。
$ErrorActionPreference = 'Stop'
wsl -d Ubuntu-22.04 -- systemctl start xiaoya-livekit xiaoya-speech
if ($LASTEXITCODE) { throw 'WSL 服务启动失败，请检查 journalctl 日志。' }
# 本地语音先完成 CUDA 预热，DeepSeek 鉴权与正文检查通过后才接入房间。
$agentStartup = @'
set -euo pipefail
deadline=$((SECONDS + 300))
until curl --silent --fail --max-time 2 http://127.0.0.1:8001/health > /dev/null; do
    if (( SECONDS >= deadline )); then
        echo '本地语音模型预热超时，请检查 xiaoya-speech 日志。' >&2
        exit 1
    fi
    sleep 1
done
systemctl stop xiaoya-agent
cd /opt/xiaoya/agent
.venv/bin/python /opt/xiaoya/prewarm-llm.py
exec .venv/bin/python -m livekit.agents start src/xiaoya/interfaces/cli.py
'@
wsl -d Ubuntu-22.04 -- bash -lc $agentStartup
if ($LASTEXITCODE) { throw 'Agent 退出，请检查终端日志。' }
