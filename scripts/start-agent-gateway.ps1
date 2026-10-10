[CmdletBinding()]
param(
    [switch]$NoVerify
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ConfigPath = Join-Path $Root 'config\gateway.json'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "gateway configuration is missing: $ConfigPath"
}
$GatewayConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$ListenHost = [string]$GatewayConfig.listen_host
$CorePort = [int]$GatewayConfig.ports.core
$AgentPort = [int]$GatewayConfig.ports.agent
$ResponsesPort = [int]$GatewayConfig.ports.responses
$ChatPort = [int]$GatewayConfig.ports.chat
$MessagesPort = [int]$GatewayConfig.ports.messages
$Ports = @($CorePort, $AgentPort, $ResponsesPort, $ChatPort, $MessagesPort)
if ([string]::IsNullOrWhiteSpace($ListenHost) -or @($Ports | Sort-Object -Unique).Count -ne 5) {
    throw 'gateway host and five distinct service ports must be configured'
}

$CoreDir = Join-Path $Root 'core'
$CoreSrc = Join-Path $CoreDir 'src'
$Python = Join-Path $CoreDir '.venv\Scripts\python.exe'
$ModelsPath = Join-Path $Root ([string]$GatewayConfig.models_file)
$AgentEntry = Join-Path $Root 'tools\agent-gateway.py'
$ResponsesEntry = Join-Path $Root 'tools\responses-proxy.py'
$ChatEntry = Join-Path $Root 'tools\chat-completions-proxy.py'
$MessagesEntry = Join-Path $Root 'tools\messages-proxy.py'
$RuntimeDir = Join-Path $CoreDir '.run'
$CoreUrl = "http://${ListenHost}:$CorePort"
$AgentUrl = "http://${ListenHost}:$AgentPort"
$ResponsesUrl = "http://${ListenHost}:$ResponsesPort"
$ChatUrl = "http://${ListenHost}:$ChatPort"
$MessagesUrl = "http://${ListenHost}:$MessagesPort"
$ControlPlaneBackend = [string]$GatewayConfig.control_plane_backend

foreach ($requiredPath in @($ModelsPath, $AgentEntry, $ResponsesEntry, $ChatEntry, $MessagesEntry)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "required gateway file is missing: $requiredPath"
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $Uv) {
        throw 'Gateway environment is missing; install uv or run uv sync --project core --locked --group dev.'
    }
    & $Uv.Source sync --project $CoreDir --locked --group dev
    if ($LASTEXITCODE -ne 0) {
        throw "dependency synchronization failed with exit code $LASTEXITCODE"
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "gateway Python environment was not created: $Python"
}
New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

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

function Wait-HttpReady {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Label,
        [int]$Attempts = 40
    )
    for ($Attempt = 0; $Attempt -lt $Attempts; $Attempt++) {
        try {
            $Response = Invoke-WebRequest -Uri $Uri -Method Get -TimeoutSec 2 -UseBasicParsing
            if ([int]$Response.StatusCode -eq 200) { return }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "$Label was not ready: $Uri"
}

function Get-CatalogIds {
    param([Parameter(Mandatory)][object]$Catalog)
    if ($null -eq $Catalog -or $null -eq $Catalog.data) { throw 'model catalog has no data array' }
    return @(
        $Catalog.data |
            Where-Object { $null -ne $_ -and $_.id -is [string] } |
            ForEach-Object { [string]$_.id } |
            Sort-Object -Unique
    )
}

function Assert-CatalogMatches {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Actual,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Expected
    )
    if ((@($Actual | Sort-Object -Unique) -join "`n") -cne (@($Expected | Sort-Object -Unique) -join "`n")) {
        throw "$Label model catalog does not match the configured protocol routes"
    }
}

function Show-LogTail {
    param([Parameter(Mandatory)][string]$Label, [Parameter(Mandatory)][string]$Path)
    Write-Host "--- $Label ---"
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Get-Content -LiteralPath $Path -Tail 30 -ErrorAction SilentlyContinue
    }
}

$CoreOut = Join-Path $RuntimeDir 'core.out.log'
$CoreErr = Join-Path $RuntimeDir 'core.err.log'
$AgentOut = Join-Path $RuntimeDir 'agent.out.log'
$AgentErr = Join-Path $RuntimeDir 'agent.err.log'
$ChatOut = Join-Path $RuntimeDir 'chat.out.log'
$ChatErr = Join-Path $RuntimeDir 'chat.err.log'
$MessagesOut = Join-Path $RuntimeDir 'messages.out.log'
$MessagesErr = Join-Path $RuntimeDir 'messages.err.log'
$ResponsesOut = Join-Path $RuntimeDir 'responses.out.log'
$ResponsesErr = Join-Path $RuntimeDir 'responses.err.log'
$CorePid = Join-Path $RuntimeDir 'core.pid'
$AgentPid = Join-Path $RuntimeDir 'agent.pid'
$ChatPid = Join-Path $RuntimeDir 'chat.pid'
$MessagesPid = Join-Path $RuntimeDir 'messages.pid'
$ResponsesPid = Join-Path $RuntimeDir 'responses.pid'

$ModelSetsJson = & $Python (Join-Path $Root 'tools\protocol_models.py') $ModelsPath | Out-String
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ModelSetsJson)) {
    throw 'model route validation failed'
}
$ModelSets = $ModelSetsJson | ConvertFrom-Json
$ExpectedResponses = @($ModelSets.responses | ForEach-Object { [string]$_ })
$ExpectedChat = @($ModelSets.chat | ForEach-Object { [string]$_ })
$ExpectedMessages = @($ModelSets.messages | ForEach-Object { [string]$_ })
$ExpectedUnified = @(@($ExpectedResponses) + @($ExpectedChat) | Sort-Object -Unique)

# 用户运行此脚本时，只替换由当前 checkout 持有的 listener。
Stop-OwnedPort -Port $AgentPort -Label 'unified Agent entry'
Stop-OwnedPort -Port $ResponsesPort -Label 'Responses protocol service'
Stop-OwnedPort -Port $ChatPort -Label 'Chat Completions service'
Stop-OwnedPort -Port $MessagesPort -Label 'Anthropic Messages service'
Stop-OwnedPort -Port $CorePort -Label 'model routing core'

$Processes = @()
try {
    $PreviousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($PreviousPythonPath)) {
            $CoreSrc
        } else {
            "$CoreSrc$([IO.Path]::PathSeparator)$PreviousPythonPath"
        }
        $CoreProcess = Start-Process -FilePath $Python -WorkingDirectory $CoreDir `
            -ArgumentList @('-m', 'llm_gateway.core_server', '--config', "`"$ModelsPath`"", '--host', $ListenHost, '--port', [string]$CorePort) `
            -RedirectStandardOutput $CoreOut -RedirectStandardError $CoreErr -WindowStyle Hidden -PassThru
    } finally {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    $CoreProcess.Id | Set-Content -LiteralPath $CorePid
    $Processes += $CoreProcess
    Wait-HttpReady -Uri "$CoreUrl/health/liveliness" -Label 'Core'

    $ChatProcess = Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList @($ChatEntry, '--host', $ListenHost, '--port', [string]$ChatPort, '--core-url', $CoreUrl, '--models-config', "`"$ModelsPath`"") `
        -RedirectStandardOutput $ChatOut -RedirectStandardError $ChatErr -WindowStyle Hidden -PassThru
    $ChatProcess.Id | Set-Content -LiteralPath $ChatPid
    $Processes += $ChatProcess
    Wait-HttpReady -Uri "$ChatUrl/health/liveliness" -Label 'Chat Completions service'

    $ResponsesProcess = Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList @($ResponsesEntry, '--host', $ListenHost, '--port', [string]$ResponsesPort, '--core-url', $CoreUrl, '--control-plane-backend', "`"$ControlPlaneBackend`"", '--models-config', "`"$ModelsPath`"") `
        -RedirectStandardOutput $ResponsesOut -RedirectStandardError $ResponsesErr -WindowStyle Hidden -PassThru
    $ResponsesProcess.Id | Set-Content -LiteralPath $ResponsesPid
    $Processes += $ResponsesProcess
    Wait-HttpReady -Uri "$ResponsesUrl/health/liveliness" -Label 'Responses service'

    $MessagesProcess = Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList @($MessagesEntry, '--host', $ListenHost, '--port', [string]$MessagesPort, '--core-url', $CoreUrl, '--models-config', "`"$ModelsPath`"") `
        -RedirectStandardOutput $MessagesOut -RedirectStandardError $MessagesErr -WindowStyle Hidden -PassThru
    $MessagesProcess.Id | Set-Content -LiteralPath $MessagesPid
    $Processes += $MessagesProcess
    Wait-HttpReady -Uri "$MessagesUrl/health/liveliness" -Label 'Anthropic Messages service'

    $AgentProcess = Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList @($AgentEntry, '--host', $ListenHost, '--port', [string]$AgentPort, '--core-url', $CoreUrl, '--responses-url', $ResponsesUrl, '--chat-url', $ChatUrl, '--models-config', "`"$ModelsPath`"") `
        -RedirectStandardOutput $AgentOut -RedirectStandardError $AgentErr -WindowStyle Hidden -PassThru
    $AgentProcess.Id | Set-Content -LiteralPath $AgentPid
    $Processes += $AgentProcess
    Wait-HttpReady -Uri "$AgentUrl/health/liveliness" -Label 'Unified Agent entry'

    if (-not $NoVerify) {
        $AgentCatalog = Invoke-RestMethod -Uri "$AgentUrl/v1/models" -TimeoutSec 20
        Assert-CatalogMatches -Label 'Agent entry' -Actual @(Get-CatalogIds $AgentCatalog) -Expected $ExpectedUnified
        $ResponsesCatalog = Invoke-RestMethod -Uri "$ResponsesUrl/v1/models" -TimeoutSec 20
        Assert-CatalogMatches -Label 'Responses service' -Actual @(Get-CatalogIds $ResponsesCatalog) -Expected $ExpectedResponses
        $ChatCatalog = Invoke-RestMethod -Uri "$ChatUrl/v1/models" -TimeoutSec 20
        Assert-CatalogMatches -Label 'Chat service' -Actual @(Get-CatalogIds $ChatCatalog) -Expected $ExpectedChat
        $MessagesCatalog = Invoke-RestMethod -Uri "$MessagesUrl/v1/models" -TimeoutSec 20
        Assert-CatalogMatches -Label 'Anthropic Messages service' -Actual @(Get-CatalogIds $MessagesCatalog) -Expected $ExpectedMessages
    }
} catch {
    Show-LogTail -Label 'Core stderr' -Path $CoreErr
    Show-LogTail -Label 'Chat stderr' -Path $ChatErr
    Show-LogTail -Label 'Responses service stderr' -Path $ResponsesErr
    Show-LogTail -Label 'Anthropic Messages service stderr' -Path $MessagesErr
    Show-LogTail -Label 'Agent entry stderr' -Path $AgentErr
    foreach ($Item in @(
        @{ Port = $AgentPort; Label = 'unified Agent entry' },
        @{ Port = $ResponsesPort; Label = 'Responses protocol service' },
        @{ Port = $ChatPort; Label = 'Chat Completions service' },
        @{ Port = $MessagesPort; Label = 'Anthropic Messages service' },
        @{ Port = $CorePort; Label = 'model routing core' }
    )) {
        try { Stop-OwnedPort -Port $Item.Port -Label $Item.Label } catch { Write-Warning "cleanup failed for $($Item.Label)" }
    }
    throw
}

Write-Host "Gateway ready: Core=$CoreUrl Agent=$AgentUrl Responses=$ResponsesUrl Chat=$ChatUrl Messages=$MessagesUrl"
