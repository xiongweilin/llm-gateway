# Stop the protocol ingress processes and the LiteLLM routing core.
# The filename is retained for existing operator and migration references.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$rootPattern = [regex]::Escape($Root)
$runtimeRoot = Join-Path $Root 'litellm'

$pidFiles = @(
    (Join-Path $runtimeRoot '.litellm-core.pid'),
    (Join-Path $runtimeRoot '.responses-ingress.pid'),
    (Join-Path $runtimeRoot '.chat-completions-ingress.pid'),
    # legacy cleanup: files written by older revisions
    (Join-Path $runtimeRoot '.agent-gateway.pid'),
    (Join-Path $runtimeRoot '.responses-proxy.pid'),
    (Join-Path $runtimeRoot '.chat-completions-proxy.pid'),
    (Join-Path $runtimeRoot '.agent-proxy.pid')
)

function Get-CommandLine([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) { return '' }
    return [string]$process.CommandLine
}

function Test-Owned([string]$CommandLine) {
    if ([string]::IsNullOrWhiteSpace($CommandLine) -or $CommandLine -notmatch "(?i)$rootPattern") {
        return $false
    }
    return $CommandLine -match '(?i)(run_server\.py|responses-proxy\.py|agent-zstd-proxy\.py|chat-completions-proxy\.py|config\.(runtime|agent)\.yaml)'
}

function Stop-Port([int]$Port, [string]$Label) {
    $connections = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $stopped = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($connection in $connections) {
        $processId = [int]$connection.OwningProcess
        if (-not $stopped.Add($processId)) { continue }
        $commandLine = Get-CommandLine $processId
        if (-not (Test-Owned $commandLine)) {
            Write-Warning "leaving unrelated listener on 127.0.0.1:$Port (pid=$processId)"
            continue
        }
        & taskkill /PID $processId /T /F 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "stopped $Label (pid=$processId)"
        } else {
            Write-Warning "failed to stop $Label (pid=$processId)"
        }
    }
    if ($connections.Count -eq 0) {
        Write-Host "$Label not running"
    }
}

Stop-Port -Port 4102 -Label 'Chat Completions ingress'
Stop-Port -Port 4100 -Label 'Unified protocol ingress'
Stop-Port -Port 4101 -Label 'LiteLLM core'

foreach ($pidFile in $pidFiles) {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { continue }
    $value = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($value -match '^\d+$') {
        $processId = [int]$value
        $commandLine = Get-CommandLine $processId
        if (Test-Owned $commandLine) {
            & taskkill /PID $processId /T /F 2>$null | Out-Null
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 500
$remaining = @(
    4100, 4101, 4102 |
        Where-Object {
            $null -ne (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $_ -State Listen -ErrorAction SilentlyContinue)
        }
)
if ($remaining.Count -eq 0) {
    Write-Host 'protocol ingress and LiteLLM core stopped'
} else {
    Write-Warning "ports still listening: $($remaining -join ', ')"
}
