#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Model = 'gpt-6-luna'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$GatewayConfigPath = Join-Path $Root 'config\gateway.json'
$GatewayConfig = Get-Content -LiteralPath $GatewayConfigPath -Raw | ConvertFrom-Json
$ListenHost = [string]$GatewayConfig.listen_host
$AgentPort = [int]$GatewayConfig.ports.agent
$GatewayBaseUrl = 'http://' + $ListenHost + ':' + $AgentPort + '/v1'

$CodexHome = if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
    [Environment]::ExpandEnvironmentVariables($env:CODEX_HOME)
} else {
    Join-Path $env:USERPROFILE '.codex'
}
$ConfigPath = Join-Path $CodexHome 'config.toml'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Codex config not found: $ConfigPath"
}

$text = [IO.File]::ReadAllText($ConfigPath)
$hasCrLf = [regex]::IsMatch($text, '\r\n')
$nl = if ($hasCrLf) { ([string][char]13 + [char]10) } else { [string][char]10 }
$finalNewline = $text.EndsWith($nl)

$text = [regex]::Replace(
    $text,
    '(?ms)^\[model_providers\.llm-gateway\]\s*\r?\n.*?(?=^\[[^\]]+\]\s*$|\z)',
    ''
)

$parts = [regex]::Split($text, '\r?\n')
if ($finalNewline -and $parts.Count -gt 1 -and $parts[$parts.Count - 1] -eq '') {
    $parts = $parts[0..($parts.Count - 2)]
}
$lines = [System.Collections.Generic.List[string]]::new()
foreach ($line in $parts) { $null = $lines.Add($line) }

$firstSection = $lines.Count
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^\s*\[[^\]]+\]\s*$') {
        $firstSection = $i
        break
    }
}

foreach ($key in @('model', 'model_provider', 'openai_base_url')) {
    $hits = @(
        $lines |
            Where-Object { $_ -match ('^\s*' + [regex]::Escape($key) + '\s*=') }
    )
    if ($hits.Count -gt 1) {
        throw "duplicate top-level Codex setting: $key"
    }
}

for ($i = $firstSection - 1; $i -ge 0; $i--) {
    if ($lines[$i] -match '^\s*openai_base_url\s*=') {
        $lines.RemoveAt($i)
        $firstSection--
    }
}

$modelIndex = -1
$providerIndex = -1
for ($i = 0; $i -lt $firstSection; $i++) {
    if ($lines[$i] -match '^\s*model\s*=') {
        $modelIndex = $i
    } elseif ($lines[$i] -match '^\s*model_provider\s*=') {
        $providerIndex = $i
    }
}
if ($modelIndex -ge 0) {
    $lines[$modelIndex] = 'model = "' + $Model + '"'
} else {
    $lines.Insert($firstSection, 'model = "' + $Model + '"')
    $modelIndex = $firstSection
    $firstSection++
}
if ($providerIndex -ge 0) {
    $lines[$providerIndex] = 'model_provider = "llm-gateway"'
} else {
    $lines.Insert($modelIndex + 1, 'model_provider = "llm-gateway"')
}

while ($lines.Count -gt 0 -and [string]::IsNullOrWhiteSpace($lines[$lines.Count - 1])) {
    $lines.RemoveAt($lines.Count - 1)
}
if ($lines.Count -gt 0) { $null = $lines.Add('') }
foreach ($line in @(
    '[model_providers.llm-gateway]',
    'name = "LLM Gateway"',
    'base_url = "' + $GatewayBaseUrl + '"',
    'wire_api = "responses"',
    'requires_openai_auth = true',
    'supports_websockets = false'
)) {
    $null = $lines.Add($line)
}

$updated = [string]::Join($nl, $lines.ToArray())
if ($finalNewline) { $updated += $nl }

$tempPath = "$ConfigPath.tmp-$PID"
$backupPath = "$ConfigPath.bak-gateway-$PID"
$utf8NoBom = [Text.UTF8Encoding]::new($false)
try {
    Copy-Item -LiteralPath $ConfigPath -Destination $backupPath -Force
    [IO.File]::WriteAllText($tempPath, $updated, $utf8NoBom)
    $candidate = [IO.File]::ReadAllText($tempPath)

    foreach ($required in @(
        'model_provider = "llm-gateway"',
        '[model_providers.llm-gateway]',
        'base_url = "' + $GatewayBaseUrl + '"',
        'wire_api = "responses"',
        'requires_openai_auth = true',
        'supports_websockets = false'
    )) {
        if (-not $candidate.Contains($required, [StringComparison]::Ordinal)) {
            throw "generated Codex config is missing: $required"
        }
    }
    if ($candidate -match '(?m)^\s*openai_base_url\s*=') {
        throw 'generated Codex config still contains top-level openai_base_url'
    }

    Move-Item -LiteralPath $tempPath -Destination $ConfigPath -Force
    Remove-Item -LiteralPath $backupPath -Force
} catch {
    if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
        Copy-Item -LiteralPath $backupPath -Destination $ConfigPath -Force
    }
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    throw
}

Write-Host "Codex gateway provider configured: $GatewayBaseUrl ($Model)"
Write-Host 'Restart Codex so it reloads the provider configuration and current ChatGPT login.'
