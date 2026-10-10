# 监控 model core、统一 Agent 入口以及各协议服务。
# 为兼容现有 scheduled task，保留此文件名。

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
$GatewayConfigPath = Join-Path $Root 'config\gateway.json'
$GatewayConfig = Get-Content -LiteralPath $GatewayConfigPath -Raw | ConvertFrom-Json
$ListenHost = [string]$GatewayConfig.listen_host
$ServicePorts = @(
    [int]$GatewayConfig.ports.core,
    [int]$GatewayConfig.ports.agent,
    [int]$GatewayConfig.ports.responses,
    [int]$GatewayConfig.ports.chat,
    [int]$GatewayConfig.ports.messages
)
$PwshExe = Join-Path $PSHOME 'pwsh.exe'
$LogRoot = Join-Path ([IO.Path]::GetTempPath()) 'llm-gateway-watchdog'
$LogPath = Join-Path $LogRoot 'watchdog.log'
$MutexName = 'Local\LLM-Gateway-Watchdog'

if (-not (Test-Path -LiteralPath $StartScript -PathType Leaf)) {
    throw "startup script is missing: $StartScript"
}
if (-not (Test-Path -LiteralPath $PwshExe -PathType Leaf)) {
    throw "PowerShell 7 is missing: $PwshExe"
}

New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

function Write-WatchdogLog([string]$Message) {
    try {
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            if ((Get-Item -LiteralPath $LogPath).Length -gt 1MB) {
                Remove-Item -LiteralPath $LogPath -Force -ErrorAction SilentlyContinue
            }
        }
        Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format o) $Message" -Encoding utf8
    } catch {
        # 不能因为监控日志不可用而终止 recovery。
    }
}

function Test-GatewayHealthy {
    foreach ($port in $ServicePorts) {
        $uri = "http://${ListenHost}:$port/health/liveliness"
        try {
            $response = Invoke-WebRequest -Uri $uri -TimeoutSec 3 -SkipHttpErrorCheck
            if ([int]$response.StatusCode -ne 200) { return $false }
        } catch {
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
                '-File', "`"$StartScript`""
            ) `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        return [int]$process.ExitCode
    } catch {
        Write-WatchdogLog 'gateway recovery process failed to start'
        return 1
    } finally {
        Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

$mutex = $null
$ownsMutex = $false
try {
    $mutex = [Threading.Mutex]::new($false, $MutexName)
    try {
        $ownsMutex = $mutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        Write-WatchdogLog 'another watcher is already active'
        return
    }

    $lastHealthy = $null
    do {
        $healthy = Test-GatewayHealthy
        if (-not $healthy) {
            if ($lastHealthy -ne $false) {
                Write-WatchdogLog 'gateway health failed'
                Write-WatchdogLog 'gateway recovery started'
            }
            $exitCode = Invoke-GatewayStart
            if ($exitCode -eq 0 -and (Test-GatewayHealthy)) {
                Write-WatchdogLog 'gateway recovery succeeded'
                $healthy = $true
            } else {
                Write-WatchdogLog "gateway recovery incomplete; startup exit=$exitCode"
                $healthy = $false
            }
        } elseif ($lastHealthy -eq $false) {
            Write-WatchdogLog 'gateway health restored'
        }

        $lastHealthy = $healthy
        if ($Once) { break }
        Start-Sleep -Seconds $PollSeconds
    } while ($true)
} catch {
    Write-WatchdogLog 'gateway watcher terminated unexpectedly'
    exit 1
} finally {
    if ($ownsMutex -and $null -ne $mutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
