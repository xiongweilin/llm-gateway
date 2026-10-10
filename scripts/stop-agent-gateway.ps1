[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ConfigPath = Join-Path $Root 'config\gateway.json'
$GatewayConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$ListenHost = [string]$GatewayConfig.listen_host
$Ports = @(
    @{ Port = [int]$GatewayConfig.ports.agent; Label = 'unified Agent entry' },
    @{ Port = [int]$GatewayConfig.ports.responses; Label = 'Responses protocol service' },
    @{ Port = [int]$GatewayConfig.ports.chat; Label = 'Chat Completions service' },
    @{ Port = [int]$GatewayConfig.ports.messages; Label = 'Anthropic Messages service' },
    @{ Port = [int]$GatewayConfig.ports.core; Label = 'model routing core' }
)

function Get-PortOwner {
    param([Parameter(Mandatory)][int]$Port)
    Get-NetTCPConnection -LocalAddress $ListenHost -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-ProcessCommandLine {
    param([Parameter(Mandatory)][int]$ProcessId)
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $Process) { return '' }
    return [string]$Process.CommandLine
}

function Test-OwnedCommand {
    param([Parameter(Mandatory)][string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    $RootPattern = [regex]::Escape($Root)
    if ($CommandLine -notmatch "(?i)$RootPattern") { return $false }
    return $CommandLine -match '(?i)(llm_gateway\.core_server|agent-gateway\.py|responses-proxy\.py|run_server\.py|chat-completions-proxy\.py|messages-proxy\.py)'
}

function Stop-OwnedPort {
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Label
    )
    $Connection = Get-PortOwner -Port $Port
    if ($null -eq $Connection) { return }
    $ListenerPid = [int]$Connection.OwningProcess
    if (-not (Test-OwnedCommand (Get-ProcessCommandLine $ListenerPid))) {
        throw "$ListenHost`:$Port is occupied by an unrelated process (pid=$ListenerPid)"
    }
    Write-Host "stopping $Label (pid=$ListenerPid)"
    & taskkill /PID $ListenerPid /T /F 2>$null | Out-Null
    Start-Sleep -Milliseconds 500
    if ($null -ne (Get-PortOwner -Port $Port)) {
        throw "$Label still owns port $Port after stop"
    }
}

foreach ($Item in $Ports) {
    Stop-OwnedPort -Port $Item.Port -Label $Item.Label
}

Write-Host 'Gateway processes stopped.'
