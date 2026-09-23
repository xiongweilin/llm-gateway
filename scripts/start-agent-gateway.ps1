# Start the unified public protocol ingress and the LiteLLM routing core.
#
#   Unified Responses/Chat ingress -> 127.0.0.1:4100
#   LiteLLM routing core            -> 127.0.0.1:4101
#
# The filename is retained for existing scheduled-task and operator references.
# Its public behavior and output are protocol-neutral.

[CmdletBinding()]
param(
    [switch]$NoVerify
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RuntimeConfig = Join-Path $Root 'litellm\config.runtime.yaml'
$RuntimeTemplate = Join-Path $Root 'litellm\config.runtime.template.yaml'
$ModelSource = Join-Path $Root 'litellm\models.yaml'
$GenerateRuntimeConfig = Join-Path $Root 'scripts\generate-runtime-config.ps1'
$Python = Join-Path $Root 'litellm\.venv\Scripts\python.exe'
$ServerEntry = Join-Path $Root 'litellm\run_server.py'
$ResponsesIngress = Join-Path $Root 'tools\responses-proxy.py'
$ChatBackend = 'http://127.0.0.1:4101'

$CoreOut = Join-Path $Root 'litellm\.litellm-core.out.log'
$CoreErr = Join-Path $Root 'litellm\.litellm-core.err.log'
$ResponsesOut = Join-Path $Root 'litellm\.responses-ingress.out.log'
$ResponsesErr = Join-Path $Root 'litellm\.responses-ingress.err.log'

$CorePidFile = Join-Path $Root 'litellm\.litellm-core.pid'
$ResponsesPidFile = Join-Path $Root 'litellm\.responses-ingress.pid'

$ControlPlaneBackend = if ([string]::IsNullOrWhiteSpace($env:CHATGPT_API_BASE)) {
    'https://chatgpt.com/backend-api/codex'
} else {
    $env:CHATGPT_API_BASE.TrimEnd('/')
}


function Get-PortOwner {
    param([Parameter(Mandatory)][int]$Port)

    Get-NetTCPConnection `
        -LocalAddress '127.0.0.1' `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
}


function Get-ProcessCommandLine {
    param([Parameter(Mandatory)][int]$ProcessId)

    $process = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$ProcessId" `
        -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return ''
    }
    return [string]$process.CommandLine
}


function Test-OwnedCommand {
    param([Parameter(Mandatory)][string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    $rootPattern = [regex]::Escape($Root)
    if ($CommandLine -notmatch "(?i)$rootPattern") {
        return $false
    }
    return $CommandLine -match '(?i)(run_server\.py|responses-proxy\.py|agent-zstd-proxy\.py|chat-completions-proxy\.py|config\.(runtime|agent)\.yaml)'
}


function Stop-OwnedPort {
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Label
    )

    $connection = Get-PortOwner -Port $Port
    if ($null -eq $connection) {
        return
    }

    $listenerPid = [int]$connection.OwningProcess
    $listenerCommand = Get-ProcessCommandLine -ProcessId $listenerPid
    if (-not (Test-OwnedCommand -CommandLine $listenerCommand)) {
        throw "127.0.0.1:$Port is occupied by an unrelated process (pid=$listenerPid)"
    }

    $killPid = $listenerPid
    $listener = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$listenerPid" `
        -ErrorAction SilentlyContinue
    if ($null -ne $listener) {
        $parent = Get-CimInstance Win32_Process `
            -Filter "ProcessId=$($listener.ParentProcessId)" `
            -ErrorAction SilentlyContinue
        if ($null -ne $parent -and (Test-OwnedCommand -CommandLine ([string]$parent.CommandLine))) {
            $killPid = [int]$parent.ProcessId
        }
    }

    Write-Host "stopping $Label (pid=$killPid)"
    & taskkill /PID $killPid /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "failed to stop $Label (pid=$killPid)"
    }

    Start-Sleep -Milliseconds 500
    if ($null -ne (Get-PortOwner -Port $Port)) {
        throw "$Label still owns port $Port after stop"
    }
}


function Remove-RuntimeArtifacts {
    foreach ($path in @(
        $CorePidFile,
        $ResponsesPidFile,
        $CoreOut,
        $CoreErr,
        $ResponsesOut,
        $ResponsesErr,
        # legacy cleanup: artifacts written by older revisions
        (Join-Path $Root 'litellm\.chat-completions-ingress.pid'),
        (Join-Path $Root 'litellm\.chat-completions-ingress.out.log'),
        (Join-Path $Root 'litellm\.chat-completions-ingress.err.log'),
        (Join-Path $Root 'litellm\.agent-gateway.pid'),
        (Join-Path $Root 'litellm\.agent-proxy.pid'),
        (Join-Path $Root 'litellm\.responses-proxy.pid'),
        (Join-Path $Root 'litellm\.chat-completions-proxy.pid'),
        (Join-Path $Root 'litellm\.agent-gateway.out.log'),
        (Join-Path $Root 'litellm\.agent-gateway.err.log'),
        (Join-Path $Root 'litellm\.responses-proxy.out.log'),
        (Join-Path $Root 'litellm\.responses-proxy.err.log'),
        (Join-Path $Root 'litellm\.chat-completions-proxy.out.log'),
        (Join-Path $Root 'litellm\.chat-completions-proxy.err.log')
    )) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}


function Show-LogTail {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$Path,
        [int]$Lines = 30
    )

    Write-Host "--- $Label ---"
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue
    } else {
        Write-Host '(log unavailable)'
    }
}


function Wait-HttpReady {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Label,
        [int]$Attempts = 60
    )

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        try {
            $null = Invoke-WebRequest -Uri $Uri -Method Get -TimeoutSec 2 -UseBasicParsing
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "$Label was not ready: $Uri"
}


function Get-CatalogIds {
    param([Parameter(Mandatory)][object]$Catalog)

    if ($null -eq $Catalog -or $null -eq $Catalog.data) {
        throw 'model catalog has no data array'
    }
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
        [Parameter(Mandatory)][AllowNull()][AllowEmptyCollection()][string[]]$Actual,
        [Parameter(Mandatory)][AllowNull()][AllowEmptyCollection()][string[]]$Expected
    )

    $actualKey = (@($Actual | Sort-Object -Unique) -join "`n")
    $expectedKey = (@($Expected | Sort-Object -Unique) -join "`n")
    if ($actualKey -cne $expectedKey) {
        throw "$Label model catalog does not match runtime protocol assignment"
    }
}


foreach ($requiredPath in @(
    $Python,
    $ServerEntry,
    $ResponsesIngress,
    $ModelSource,
    $RuntimeTemplate,
    $GenerateRuntimeConfig
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "required gateway file is missing: $requiredPath"
    }
}

# Generate and validate the runtime configuration before touching live processes.
& $GenerateRuntimeConfig `
    -ModelsPath $ModelSource `
    -TemplatePath $RuntimeTemplate `
    -OutputPath $RuntimeConfig `
    -PythonPath $Python
if (-not (Test-Path -LiteralPath $RuntimeConfig -PathType Leaf)) {
    throw 'runtime configuration generation did not produce the output file'
}

$protocolJson = & $Python (Join-Path $Root 'tools\protocol_models.py') $RuntimeConfig | Out-String
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($protocolJson)) {
    throw 'runtime protocol model validation failed'
}
$protocolModels = $protocolJson | ConvertFrom-Json
$ExpectedResponses = @($protocolModels.responses | ForEach-Object { [string]$_ })
$ExpectedChat = @($protocolModels.chat | ForEach-Object { [string]$_ })
$ExpectedUnified = @(
    @($ExpectedResponses) + @($ExpectedChat) |
        ForEach-Object { [string]$_ } |
        Sort-Object -Unique
)

Stop-OwnedPort -Port 4102 -Label 'legacy Chat Completions ingress'
Stop-OwnedPort -Port 4100 -Label 'Unified protocol ingress'
Stop-OwnedPort -Port 4101 -Label 'LiteLLM core'
Remove-RuntimeArtifacts

$core = $null
$responses = $null
try {
    $core = Start-Process `
        -FilePath $Python `
        -WorkingDirectory $Root `
        -ArgumentList @($ServerEntry, '--config', $RuntimeConfig, '--host', '127.0.0.1', '--port', '4101') `
        -RedirectStandardOutput $CoreOut `
        -RedirectStandardError $CoreErr `
        -WindowStyle Hidden `
        -PassThru
    $core.Id | Set-Content -LiteralPath $CorePidFile
    Wait-HttpReady -Uri 'http://127.0.0.1:4101/health/liveliness' -Label 'LiteLLM core'
    $coreOwner = Get-PortOwner -Port 4101
    if ($null -eq $coreOwner) {
        throw 'LiteLLM core health check passed without a listening process'
    }
    ([int]$coreOwner.OwningProcess) | Set-Content -LiteralPath $CorePidFile
    Write-Host 'LiteLLM core ready (127.0.0.1:4101)'

    $responses = Start-Process `
        -FilePath $Python `
        -WorkingDirectory $Root `
        -ArgumentList @($ResponsesIngress, '4100', 'http://127.0.0.1:4101', $ControlPlaneBackend, $RuntimeConfig, $ChatBackend) `
        -RedirectStandardOutput $ResponsesOut `
        -RedirectStandardError $ResponsesErr `
        -WindowStyle Hidden `
        -PassThru
    $responses.Id | Set-Content -LiteralPath $ResponsesPidFile
    Wait-HttpReady -Uri 'http://127.0.0.1:4100/health/liveliness' -Label 'Unified protocol ingress' -Attempts 40
    $responsesOwner = Get-PortOwner -Port 4100
    if ($null -eq $responsesOwner) {
        throw 'Unified protocol ingress health check passed without a listening process'
    }
    ([int]$responsesOwner.OwningProcess) | Set-Content -LiteralPath $ResponsesPidFile
    Write-Host 'Unified protocol ingress ready (127.0.0.1:4100)'

    if (-not $NoVerify) {
        $responseCatalog = Invoke-RestMethod -Uri 'http://127.0.0.1:4100/v1/models' -TimeoutSec 30
        Assert-CatalogMatches -Label 'Unified protocol ingress' -Actual (Get-CatalogIds $responseCatalog) -Expected $ExpectedUnified
    }
} catch {
    Show-LogTail -Label 'LiteLLM core stderr' -Path $CoreErr
    Show-LogTail -Label 'Unified protocol ingress stderr' -Path $ResponsesErr
    foreach ($cleanup in @(
        @{ Port = 4102; Label = 'legacy Chat Completions ingress' },
        @{ Port = 4100; Label = 'Unified protocol ingress' },
        @{ Port = 4101; Label = 'LiteLLM core' }
    )) {
        try {
            Stop-OwnedPort -Port $cleanup.Port -Label $cleanup.Label
        } catch {
            Write-Warning "cleanup failed for $($cleanup.Label)"
        }
    }
    Remove-RuntimeArtifacts
    throw
}

Write-Host 'Protocol ingress startup complete'
exit 0
