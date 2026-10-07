# 使用隐藏进程保留本地网页服务；只在指定端口空闲时启动，避免重复实例。
param([int]$Port = 3000, [string]$WslDistro = 'Ubuntu-22.04')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$webRoot = Join-Path $projectRoot 'web'
$logRoot = Join-Path $projectRoot '.tools/logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "端口 $Port 已占用，请检查现有服务。"
}
# WSL NAT 地址会在重启后变化；显式读取 eth0，避免依赖不稳定的 localhost 信令转发。
$addressOutput = wsl -d $WslDistro -- ip -4 -o addr show dev eth0
if ($LASTEXITCODE) { throw '读取 WSL 网卡地址失败。' }
$addressMatch = [regex]::Match(($addressOutput -join "`n"), '\binet\s+(\d+\.\d+\.\d+\.\d+)/')
if (-not $addressMatch.Success) { throw 'WSL eth0 没有可用的 IPv4 地址。' }
$envPath = Join-Path $webRoot '.env.local'
$envContent = Get-Content -LiteralPath $envPath -Raw
$livekitUrl = "ws://$($addressMatch.Groups[1].Value):7880"
if ($envContent -notmatch '(?m)^LIVEKIT_URL=') { throw 'web/.env.local 缺少 LIVEKIT_URL。' }
$envContent = [regex]::Replace($envContent, '(?m)^LIVEKIT_URL=[^\r\n]*', "LIVEKIT_URL=$livekitUrl")
Set-Content -LiteralPath $envPath -Value $envContent -NoNewline
$nextScript = Join-Path $webRoot 'node_modules/next/dist/bin/next'
$process = Start-Process -FilePath (Get-Command node.exe).Source `
    -ArgumentList @($nextScript, 'dev', '--hostname', '127.0.0.1', '--port', $Port) `
    -WorkingDirectory $webRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot 'web.stdout.log') `
    -RedirectStandardError (Join-Path $logRoot 'web.stderr.log')
Set-Content -LiteralPath (Join-Path $logRoot 'web.pid') -Value $process.Id
Write-Output "网页已启动：http://localhost:$Port（PID $($process.Id)）"
