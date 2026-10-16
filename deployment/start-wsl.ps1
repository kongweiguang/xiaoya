# 所有后端归 systemd；日志跟随只维持 WSL 生命周期，不拥有第二个 Agent。
param([string]$WslDistro = 'Ubuntu-22.04')
$ErrorActionPreference = 'Stop'

function Enter-XiaoyaLauncherMutex {
    <# 项目和发行版共同界定日志控制者；锁由操作系统持有，窗口崩溃后允许接管而不留陈旧PID锁。 #>
    $root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent)).ToLowerInvariant()
    $identity = "$root`n$($WslDistro.Trim().ToLowerInvariant())"
    $digest = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData(
        [Text.Encoding]::UTF8.GetBytes($identity))).ToLowerInvariant()
    $mutex = [Threading.Mutex]::new($false, "Local\Xiaoya.WslLauncher.$digest")
    try {
        try { $acquired = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if ($acquired) { return $mutex }
    } catch {
        $mutex.Dispose()
        throw
    }
    $mutex.Dispose()
    return $null
}

function Set-XiaoyaJevProxyAddress {
    <# 只在地址确实改变时写单个字段，重复启动不能触碰密钥或制造无意义的Agent重启。 #>
    param([string]$Gateway)
    $updateProxy = 'from dotenv import dotenv_values, set_key; import sys; p="/opt/xiaoya/agent/.env.local"; changed=dotenv_values(p).get("VOICE_AGENT_JEV_PROXY_URL") != sys.argv[1]; set_key(p, "VOICE_AGENT_JEV_PROXY_URL", sys.argv[1], quote_mode="never") if changed else None; print(int(changed))'
    $proxyChanged = wsl -d $WslDistro -- /opt/xiaoya/agent/.venv/bin/python -c $updateProxy "http://${Gateway}:10818"
    if ($LASTEXITCODE) { throw '更新 WSL Jev 代理地址失败。' }
    return ($proxyChanged -join '').Trim() -eq '1'
}

function Test-XiaoyaJevBridgeOwner {
    <# 升级兼容仅借用可确认的本项目Python桥；完整参数匹配拒绝其他程序、旧WSL来源及附加参数。 #>
    param($Process, [string]$BridgeScript, [string]$Gateway, [string]$Peer)
    if (-not $Process -or $Process.Name -ne 'python.exe' -or
        -not $Process.ExecutablePath -or
        [IO.Path]::GetFileName($Process.ExecutablePath) -ne 'python.exe' -or
        -not $Process.CommandLine) { return $false }
    $pattern = '^\s*(?:"{0}"|{0})\s+(?:"{1}"|{1})\s+--listen-host\s+{2}\s+--allowed-peer\s+{3}\s+--ready-file\s+(?:"[^"]+"|[^\s"]+)\s*$' -f
        [regex]::Escape($Process.ExecutablePath),
        [regex]::Escape([IO.Path]::GetFullPath($BridgeScript)),
        [regex]::Escape($Gateway), [regex]::Escape($Peer)
    return [regex]::IsMatch($Process.CommandLine, $pattern,
        [Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
        [Text.RegularExpressions.RegexOptions]::CultureInvariant)
}

function Start-XiaoyaJevProxyBridge {
    <# NAT只转发本项目Jev；优先验证并借用旧启动器的桥，绝不抢占端口或取得别人的清理权。 #>
    $projectRoot = Split-Path $PSScriptRoot -Parent
    $configuration = Join-Path $projectRoot '.env.local'
    if (-not (Test-Path -LiteralPath $configuration)) { return $null }
    $checkKey = 'from dotenv import dotenv_values; import sys; print(int(bool((dotenv_values(sys.argv[1]).get("VOICE_AGENT_JEV_API_KEY") or "").strip())))'
    $keyConfigured = uv run --directory $projectRoot --no-sync python -c $checkKey $configuration
    if ($LASTEXITCODE) { throw '读取本项目 Jev 配置失败。' }
    if (($keyConfigured -join '').Trim() -ne '1') { return $null }
    $route = wsl -d $WslDistro -- ip -4 route show default
    if ($LASTEXITCODE) { throw '读取 WSL 默认路由失败。' }
    $gateway = [regex]::Match(($route -join ' '), '\bvia\s+(\d+\.\d+\.\d+\.\d+)').Groups[1].Value
    $address = wsl -d $WslDistro -- ip -4 -o addr show dev eth0
    if ($LASTEXITCODE) { throw '读取 WSL 来源地址失败。' }
    $peer = [regex]::Match(($address -join ' '), '\binet\s+(\d+\.\d+\.\d+\.\d+)/').Groups[1].Value
    if (-not $gateway -or -not $peer -or
        -not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $gateway -ErrorAction SilentlyContinue)) {
        throw '未找到 Windows 的 WSL NAT 网卡，无法安全转发 Jev 代理。'
    }
    if (-not (Get-NetTCPConnection -State Listen -LocalPort 10808 -ErrorAction SilentlyContinue)) {
        throw '请先开启 Windows 10808 日本代理。'
    }
    $bridgeScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'jev_proxy_bridge.py'))
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 10818 -ErrorAction SilentlyContinue)
    if ($listeners.Count) {
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne $gateway) {
            throw '10818 已被非本项目的 WSL 代理占用，未修改或停止已有进程。'
        }
        $owner = Get-CimInstance -ClassName Win32_Process `
            -Filter "ProcessId = $($listeners[0].OwningProcess)" `
            -Property Name,ExecutablePath,CommandLine -ErrorAction SilentlyContinue
        if (-not (Test-XiaoyaJevBridgeOwner -Process $owner -BridgeScript $bridgeScript `
                -Gateway $gateway -Peer $peer)) {
            throw '10818 进程归属或 WSL 来源不匹配，未修改或停止已有进程。'
        }
        return [PSCustomObject]@{
            Borrowed = $true
            Process = $null
            Launcher = $null
            Owned = @()
            ReadyFile = $null
            ProxyChanged = Set-XiaoyaJevProxyAddress -Gateway $gateway
        }
    }
    $logRoot = Join-Path $projectRoot '.tools/logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $readyFile = Join-Path $logRoot "jev-proxy-$PID.json"
    $launchedAt = [DateTime]::UtcNow
    $launcher = Start-Process -FilePath (Get-Command uv.exe).Source -WindowStyle Hidden -PassThru `
        -ArgumentList @('run', '--no-project', '--python', '3.12', "`"$bridgeScript`"",
            '--listen-host', $gateway, '--allowed-peer', $peer, '--ready-file', "`"$readyFile`"") `
        -RedirectStandardOutput (Join-Path $logRoot "jev-proxy-$PID.stdout.log") `
        -RedirectStandardError (Join-Path $logRoot "jev-proxy-$PID.stderr.log")
    $bridgeProcess = $null
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-Path -LiteralPath $readyFile) {
                $receipt = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
                $candidate = Get-Process -Id $receipt.pid -ErrorAction SilentlyContinue
                if ($candidate -and $candidate.StartTime.ToUniversalTime() -ge $launchedAt) {
                    $bridgeProcess = $candidate
                    break
                }
            }
            if ($launcher.HasExited) { throw 'Jev 代理转发启动失败，请检查本项目代理日志。' }
            Start-Sleep -Milliseconds 100
        }
        if (-not $bridgeProcess) { throw 'Jev 代理转发启动超时。' }
        $proxyChanged = Set-XiaoyaJevProxyAddress -Gateway $gateway
        return [PSCustomObject]@{
            Borrowed = $false
            Process = $bridgeProcess
            Launcher = $launcher
            Owned = @(
                [PSCustomObject]@{ Id = $bridgeProcess.Id; Started = $bridgeProcess.StartTime.ToUniversalTime() }
                [PSCustomObject]@{ Id = $launcher.Id; Started = $launcher.StartTime.ToUniversalTime() }
            )
            ReadyFile = $readyFile
            ProxyChanged = $proxyChanged
        }
    } catch {
        if ($bridgeProcess -and -not $bridgeProcess.HasExited) { $bridgeProcess.Kill() }
        if (-not $launcher.HasExited) { $launcher.Kill() }
        Remove-Item -LiteralPath $readyFile -ErrorAction SilentlyContinue
        throw
    }
}

function Stop-XiaoyaJevProxyBridge {
    <# 借用的桥没有清理权；仅关闭本次记录创建时间的进程，不能停止VPN或旧启动器。 #>
    param($Bridge)
    if (-not $Bridge) { return }
    foreach ($owned in $Bridge.Owned) {
        $current = Get-Process -Id $owned.Id -ErrorAction SilentlyContinue
        if ($current -and $current.StartTime.ToUniversalTime() -eq $owned.Started) {
            Stop-Process -InputObject $current -ErrorAction SilentlyContinue
        }
    }
    if ($Bridge.ReadyFile) { Remove-Item -LiteralPath $Bridge.ReadyFile -ErrorAction SilentlyContinue }
}

$jevBridge = $null
$launcherMutex = $null
try {
    $launcherMutex = Enter-XiaoyaLauncherMutex
    if (-not $launcherMutex) {
        Write-Output '本项目已有启动窗口正在运行。请查看并保持原有日志窗口打开；此次启动已退出。'
        return
    }
    $jevBridge = Start-XiaoyaJevProxyBridge
    $agentType = wsl -d $WslDistro -- systemctl show xiaoya-agent --property=Type --value
    if ($LASTEXITCODE -or ($agentType -join '').Trim() -ne 'notify') {
        throw '请先部署当前 Type=notify 的 xiaoya-agent 单元，才能确认注册就绪。'
    }
    if (-not $jevBridge -or -not $jevBridge.Borrowed) {
        wsl -d $WslDistro -- systemctl start xiaoya-livekit xiaoya-speech xiaoya-agent
        if ($LASTEXITCODE) { throw 'WSL 服务启动失败，请检查 journalctl 日志。' }
    }
    if ($jevBridge -and $jevBridge.ProxyChanged) {
        wsl -d $WslDistro -- systemctl restart xiaoya-agent
        if ($LASTEXITCODE) { throw 'Agent 重载 Jev 代理配置失败。' }
    }
    # 多单元is-active在任一active时也可能返回0，必须逐项通过才能把notify状态当作注册就绪。
    $unitStates = @(wsl -d $WslDistro -- systemctl is-active xiaoya-livekit xiaoya-speech xiaoya-agent)
    if ($LASTEXITCODE -or $unitStates.Count -ne 3 -or
        @($unitStates | Where-Object { $_.Trim() -ne 'active' }).Count) {
        throw '后端未完成模型预热或 Agent 注册。'
    }
    $unitStates | Write-Output
    if ($jevBridge -and $jevBridge.Borrowed) {
        Write-Output '已确认现有本项目代理与后端就绪。请保持原有日志窗口打开；此次未创建转发或第二个日志窗口。'
        return
    }
    Write-Output '后端已就绪。请保持此窗口打开，以跟随日志并维持 WSL 运行；关闭后 WSL 可能自动退出，服务会随实例停止。'
    wsl -d $WslDistro -- journalctl --follow --unit xiaoya-livekit --unit xiaoya-speech --unit xiaoya-agent --lines 30
    if ($LASTEXITCODE) { throw 'WSL 日志跟随退出。' }
} finally {
    try { Stop-XiaoyaJevProxyBridge -Bridge $jevBridge }
    finally {
        if ($launcherMutex) {
            try { $launcherMutex.ReleaseMutex() }
            finally { $launcherMutex.Dispose() }
        }
    }
}
