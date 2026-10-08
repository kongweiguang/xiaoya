# 隐藏子进程只继承本次 WSL 地址；启动成功须由新进程存活与 HTTP 就绪共同证明。
param([ValidateRange(1, 65535)][int]$Port = 3000, [string]$WslDistro = 'Ubuntu-22.04')
$ErrorActionPreference = 'Stop'

function Get-XiaoyaProcessTree {
    <# Next dev 的监听者是子进程；按创建时间和父子关系确认本次启动拥有的完整进程树。 #>
    param([int]$RootPid, [DateTime]$StartedAt)
    $processes = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, CreationDate)
    $rootRecord = $processes | Where-Object { [int]$_.ProcessId -eq $RootPid }
    if ($rootRecord -and
        [Math]::Abs(($rootRecord.CreationDate.ToUniversalTime() - $StartedAt).TotalMilliseconds) -ge 1) {
        return
    }
    $owned = @{}
    $owned[$RootPid] = 0
    $added = $true
    while ($added) {
        $added = $false
        foreach ($process in $processes) {
            $processId = [int]$process.ProcessId
            if (-not $owned.ContainsKey($processId) -and
                $owned.ContainsKey([int]$process.ParentProcessId) -and
                $process.CreationDate.ToUniversalTime() -ge $StartedAt) {
                $owned[$processId] = $owned[[int]$process.ParentProcessId] + 1
                $added = $true
            }
        }
    }
    foreach ($process in $processes) {
        $processId = [int]$process.ProcessId
        if ($owned.ContainsKey($processId) -and
            ($processId -eq $RootPid -or $process.CreationDate.ToUniversalTime() -ge $StartedAt)) {
            [PSCustomObject]@{
                Id = $processId
                CreatedAt = $process.CreationDate.ToUniversalTime()
                Depth = $owned[$processId]
            }
        }
    }
}

function Stop-XiaoyaProcessTree {
    <# 失败只关闭本次进程树，按创建时间重查 PID，避免遗留 Next 子进程或误杀复用 PID。 #>
    param([int]$RootPid, [DateTime]$StartedAt)
    $owned = @(Get-XiaoyaProcessTree -RootPid $RootPid -StartedAt $StartedAt)
    foreach ($entry in ($owned | Sort-Object Depth -Descending)) {
        $process = Get-Process -Id $entry.Id -ErrorAction SilentlyContinue
        if ($process -and
            [Math]::Abs(($process.StartTime.ToUniversalTime() - $entry.CreatedAt).TotalMilliseconds) -lt 1) {
            Stop-Process -InputObject $process -ErrorAction SilentlyContinue
        }
    }
}

$projectRoot = Split-Path $PSScriptRoot -Parent
$webRoot = Join-Path $projectRoot 'web'
$logRoot = Join-Path $projectRoot '.tools/logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
# 日志与 PID 同样按端口分开，本次诊断不能覆盖另一网页进程的所有权记录。
$stdoutLog = Join-Path $logRoot "web-$Port.stdout.log"
$stderrLog = Join-Path $logRoot "web-$Port.stderr.log"
$pidFile = Join-Path $logRoot "web-$Port.pid"
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "端口 $Port 已占用，请检查现有服务。"
}
# WSL NAT 地址会在重启后变化；显式读取 eth0，避免依赖不稳定的 localhost 信令转发。
$addressOutput = wsl -d $WslDistro -- ip -4 -o addr show dev eth0
if ($LASTEXITCODE) { throw '读取 WSL 网卡地址失败。' }
$addressMatch = [regex]::Match(($addressOutput -join "`n"), '\binet\s+(\d+\.\d+\.\d+\.\d+)/')
if (-not $addressMatch.Success) { throw 'WSL eth0 没有可用的 IPv4 地址。' }
$livekitUrl = "ws://$($addressMatch.Groups[1].Value):7880"
$previousLivekitUrl = $env:LIVEKIT_URL
$previousDistDir = $env:NEXT_DIST_DIR
$nextScript = Join-Path $webRoot 'node_modules/next/dist/bin/next'
try {
    $env:LIVEKIT_URL = $livekitUrl
    # 同项目并行端口不能共享 Next 写缓存，避免另一进程破坏已运行页面的 SSR 模块。
    $env:NEXT_DIST_DIR = ".next-dev-$Port"
    # 角色 SDK 产物不纳入源码，在直接运行 Next 前同样执行唯一构建入口。
    Push-Location $webRoot
    try {
        pnpm sdk:build
        if ($LASTEXITCODE) { throw '角色 SDK 构建失败。' }
    } finally { Pop-Location }
    $webProcess = Start-Process -FilePath (Get-Command node.exe).Source `
        -ArgumentList @("`"$nextScript`"", 'dev', '--hostname', '127.0.0.1', '--port', $Port) `
        -WorkingDirectory $webRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
    $webStartedAt = $webProcess.StartTime.ToUniversalTime()
} finally {
    $env:LIVEKIT_URL = $previousLivekitUrl
    $env:NEXT_DIST_DIR = $previousDistDir
}
try {
    $startupDeadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $startupDeadline) {
        $webProcess.Refresh()
        if ($webProcess.HasExited) { throw "网页进程启动失败，请检查 $stderrLog。" }
        $listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port `
            -State Listen -ErrorAction SilentlyContinue)
        $owned = @(Get-XiaoyaProcessTree -RootPid $webProcess.Id -StartedAt $webStartedAt)
        if ($listeners | Where-Object { $_.OwningProcess -notin $owned.Id }) {
            throw "端口 $Port 的监听进程不属于本次网页启动。"
        }
        try {
            $response = if ($listeners.Count) {
                Invoke-WebRequest -Uri "http://127.0.0.1:$Port" -TimeoutSec 2 -SkipHttpErrorCheck
            }
            if ($response -and $response.StatusCode -eq 200) {
                $webProcess.Refresh()
                if ($webProcess.HasExited) { throw '网页进程在就绪检查期间退出。' }
                # HTTP 探测期间也可能退出并让出端口，成功前再核对实际监听归属。
                $listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port `
                    -State Listen -ErrorAction SilentlyContinue)
                $owned = @(Get-XiaoyaProcessTree -RootPid $webProcess.Id -StartedAt $webStartedAt)
                if (-not $listeners.Count -or ($listeners | Where-Object { $_.OwningProcess -notin $owned.Id })) {
                    throw '网页就绪检查期间监听进程发生变化。'
                }
                [System.IO.File]::WriteAllText($pidFile, [string]$webProcess.Id)
                Write-Output "网页已就绪：http://localhost:$Port（PID $($webProcess.Id)）"
                return
            }
        } catch [System.Net.Http.HttpRequestException] { }
          catch [System.Threading.Tasks.TaskCanceledException] { }
        Start-Sleep -Milliseconds 500
    }
    throw "网页启动超时，请检查 $stderrLog。"
} catch {
    Stop-XiaoyaProcessTree -RootPid $webProcess.Id -StartedAt $webStartedAt
    throw
}
