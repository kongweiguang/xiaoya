# 所有后端归 systemd；日志跟随只维持 WSL 生命周期，不拥有第二个 Agent。
param([string]$WslDistro = 'Ubuntu-22.04')
$ErrorActionPreference = 'Stop'
$agentType = wsl -d $WslDistro -- systemctl show xiaoya-agent --property=Type --value
if ($LASTEXITCODE -or ($agentType -join '').Trim() -ne 'notify') {
    throw '请先部署当前 Type=notify 的 xiaoya-agent 单元，才能确认注册就绪。'
}
wsl -d $WslDistro -- systemctl start xiaoya-livekit xiaoya-speech xiaoya-agent
if ($LASTEXITCODE) { throw 'WSL 服务启动失败，请检查 journalctl 日志。' }
wsl -d $WslDistro -- systemctl is-active xiaoya-livekit xiaoya-speech xiaoya-agent
if ($LASTEXITCODE) { throw '后端未完成模型预热或 Agent 注册。' }
Write-Output '后端已就绪。此窗口跟随日志并保持 WSL 运行，关闭窗口不会另行启动或停止 Agent。'
wsl -d $WslDistro -- journalctl --follow --unit xiaoya-livekit --unit xiaoya-speech --unit xiaoya-agent --lines 30
if ($LASTEXITCODE) { throw 'WSL 日志跟随退出。' }
