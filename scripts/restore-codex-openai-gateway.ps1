#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Model = 'gpt-6-luna'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$GatewayConfig = Get-Content -LiteralPath (Join-Path $Root 'config\gateway.json') -Raw | ConvertFrom-Json
$GatewayBaseUrl = 'http://' + [string]$GatewayConfig.listen_host + ':' + [int]$GatewayConfig.ports.agent + '/v1'
$CodexHome = if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
    [Environment]::ExpandEnvironmentVariables($env:CODEX_HOME)
} else {
    Join-Path $env:USERPROFILE '.codex'
}
$ConfigPath = Join-Path $CodexHome 'config.toml'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Codex config not found: $ConfigPath"
}

$before = [IO.File]::ReadAllText($ConfigPath)
$nl = if ($before.Contains(([string][char]13 + [char]10))) {
    [string][char]13 + [char]10
} else {
    [string][char]10
}
$finalNewline = $before.EndsWith($nl)

$text = [regex]::Replace(
    $before,
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

for ($i = $firstSection - 1; $i -ge 0; $i--) {
    if ($lines[$i] -match '^\s*model_provider\s*=\s*"llm-gateway"\s*$') {
        $lines.RemoveAt($i)
        $firstSection--
    }
}

$modelIndex = -1
$baseIndex = -1
for ($i = 0; $i -lt $firstSection; $i++) {
    if ($lines[$i] -match '^\s*model\s*=') {
        if ($modelIndex -ge 0) { throw 'duplicate top-level model' }
        $modelIndex = $i
    } elseif ($lines[$i] -match '^\s*openai_base_url\s*=') {
        if ($baseIndex -ge 0) { throw 'duplicate top-level openai_base_url' }
        $baseIndex = $i
    }
}
if ($modelIndex -ge 0) {
    $lines[$modelIndex] = 'model = "' + $Model + '"'
} else {
    $lines.Insert($firstSection, 'model = "' + $Model + '"')
    $modelIndex = $firstSection
    $firstSection++
}
if ($baseIndex -ge 0) {
    $lines[$baseIndex] = 'openai_base_url = "' + $GatewayBaseUrl + '"'
} else {
    $lines.Insert($modelIndex + 1, 'openai_base_url = "' + $GatewayBaseUrl + '"')
}

$after = [string]::Join($nl, $lines.ToArray())
if ($finalNewline) { $after += $nl }
$backup = "$ConfigPath.bak-before-restore-$PID"
Copy-Item -LiteralPath $ConfigPath -Destination $backup -Force
try {
    [IO.File]::WriteAllText($ConfigPath, $after, [Text.UTF8Encoding]::new($false))
    $check = [IO.File]::ReadAllText($ConfigPath)
    if ($check -match '(?m)^\s*model_provider\s*=\s*"llm-gateway"\s*$') {
        throw 'llm-gateway model_provider still present'
    }
    if ($check -match '(?m)^\[model_providers\.llm-gateway\]\s*$') {
        throw 'llm-gateway provider section still present'
    }
    if (-not $check.Contains('openai_base_url = "' + $GatewayBaseUrl + '"', [StringComparison]::Ordinal)) {
        throw 'gateway openai_base_url was not restored'
    }
    Remove-Item -LiteralPath $backup -Force
} catch {
    Copy-Item -LiteralPath $backup -Destination $ConfigPath -Force
    throw
}

Write-Host "Restored Codex built-in OpenAI provider -> $GatewayBaseUrl ($Model)"
Write-Host 'Restart Codex after restarting llm-gateway.'
