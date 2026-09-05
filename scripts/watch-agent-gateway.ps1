#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateRange(5, 60)][int]$PollSeconds = 10,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$StartScript = Join-Path $Root 'scripts\start-agent-gateway.ps1'
$PwshExe = Join-Path $PSHOME 'pwsh.exe'
$LogRoot = Join-Path ([IO.Path]::GetTempPath()) 'litellm-agent-gateway-watchdog'
$LogPath = Join-Path $LogRoot 'watchdog.log'
$MutexName = 'Local\LiteLLM-Agent-Gateway-Watchdog'

if (-not (Test-Path -LiteralPath $StartScript -PathType Leaf)) {
    throw "网关启动脚本不存在: $StartScript"
}
if (-not (Test-Path -LiteralPath $PwshExe -PathType Leaf)) {
    throw "PowerShell 7 不存在: $PwshExe"
}

New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

function Write-WatchdogLog([string]$Message) {
    try {
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            $length = (Get-Item -LiteralPath $LogPath).Length
            if ($length -gt 1MB) {
                Remove-Item -LiteralPath $LogPath -Force -ErrorAction SilentlyContinue
            }
        }
        Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format o) $Message" -Encoding utf8
    }
    catch {
        # Watchdog logging must never terminate gateway recovery.
    }
}

function Test-GatewayHealthy {
    foreach ($url in @(
        'http://127.0.0.1:4100/health/liveliness',
        'http://127.0.0.1:4101/health/liveliness'
    )) {
        try {
            $response = Invoke-WebRequest -Uri $url -TimeoutSec 3 -SkipHttpErrorCheck
            if ([int]$response.StatusCode -ne 200) {
                return $false
            }
        }
        catch {
            return $false
        }
    }
    return $true
}

function Invoke-GatewayStart {
    $attempt = Join-Path $LogRoot ([guid]::NewGuid().ToString('N'))
    $stdout = "$attempt.out.log"
    $stderr = "$attempt.err.log"
    try {
        $process = Start-Process `
            -FilePath $PwshExe `
            -WorkingDirectory $Root `
            -ArgumentList @(
                '-NoProfile',
                '-NonInteractive',
                '-ExecutionPolicy', 'Bypass',
                '-File', $StartScript
            ) `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        return [int]$process.ExitCode
    }
    catch {
        Write-WatchdogLog "启动子进程异常: $($_.Exception.GetType().Name)"
        return 1
    }
    finally {
        Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

$mutex = $null
$ownsMutex = $false
try {
    $mutex = [Threading.Mutex]::new($false, $MutexName)
    try {
        $ownsMutex = $mutex.WaitOne(0)
    }
    catch [Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        Write-WatchdogLog '检测到已有 watchdog，当前实例退出'
        return
    }

    $lastHealthy = $null
    do {
        $healthy = Test-GatewayHealthy
        if (-not $healthy) {
            if ($lastHealthy -ne $false) {
                Write-WatchdogLog '4100/4101 健康检查失败，开始恢复网关'
            }
            $exitCode = Invoke-GatewayStart
            if ($exitCode -eq 0 -and (Test-GatewayHealthy)) {
                Write-WatchdogLog '网关恢复成功'
                $healthy = $true
            }
            else {
                Write-WatchdogLog "网关恢复未完成，启动脚本退出码=$exitCode"
                $healthy = $false
            }
        }
        elseif ($lastHealthy -eq $false) {
            Write-WatchdogLog '网关健康检查恢复'
        }

        $lastHealthy = $healthy
        if ($Once) {
            break
        }
        Start-Sleep -Seconds $PollSeconds
    } while ($true)
}
catch {
    Write-WatchdogLog "watchdog 终止: $($_.Exception.GetType().Name)"
    exit 1
}
finally {
    if ($ownsMutex -and $null -ne $mutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
}
