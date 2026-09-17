# Generate the LiteLLM runtime configuration from the gateway-owned model source.

[CmdletBinding()]
param(
    [string]$ModelsPath,
    [string]$TemplatePath,
    [string]$OutputPath,
    [string]$PythonPath,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($ModelsPath)) {
    $ModelsPath = Join-Path $Root 'litellm\models.yaml'
}
if ([string]::IsNullOrWhiteSpace($TemplatePath)) {
    $TemplatePath = Join-Path $Root 'litellm\config.runtime.template.yaml'
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $Root 'litellm\config.runtime.yaml'
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $Root 'litellm\.venv\Scripts\python.exe'
}

$BeginMarker = '  # BEGIN GENERATED RUNTIME MODELS'
$EndMarker = '  # END GENERATED RUNTIME MODELS'

foreach ($path in @($ModelsPath, $TemplatePath, $PythonPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required runtime generation input is missing: $path"
    }
}

$yamlReader = @'
import json
import sys
from pathlib import Path
import yaml

source = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(source, dict) or not isinstance(source.get("models"), list):
    raise SystemExit("models.yaml must contain a models list")
json.dump(source, sys.stdout, ensure_ascii=False)
'@
$sourceJson = (& $PythonPath -c $yamlReader $ModelsPath | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceJson)) {
    throw "unable to parse runtime model source: $ModelsPath"
}
$source = $sourceJson | ConvertFrom-Json

function Quote-YamlScalar([object]$Value) {
    if ($Value -is [bool]) {
        return $(if ($Value) { 'true' } else { 'false' })
    }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) {
        return [string]$Value
    }
    $text = [string]$Value
    return "'" + $text.Replace("'", "''") + "'"
}

$lines = [System.Collections.Generic.List[string]]::new()
$seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$models = @($source.models)
if ($models.Count -eq 0) {
    throw 'runtime model source must contain at least one model'
}

foreach ($entry in $models) {
    $id = [string]$entry.id
    $mode = [string]$entry.mode
    if ([string]::IsNullOrWhiteSpace($id) -or $id -notmatch '^[A-Za-z0-9._:/-]+$') {
        throw "invalid runtime model id: $id"
    }
    if (-not $seen.Add($id)) {
        throw "duplicate runtime model id: $id"
    }
    if ($mode -notin @('responses', 'chat')) {
        throw "invalid protocol mode for ${id}: $mode"
    }
    if ($null -eq $entry.route -or [string]::IsNullOrWhiteSpace([string]$entry.route.model)) {
        throw "runtime model has no route.model: $id"
    }

    [void]$lines.Add("  - model_name: $(Quote-YamlScalar $id)")
    [void]$lines.Add('    model_info:')
    [void]$lines.Add("      mode: $mode")
    [void]$lines.Add('    litellm_params:')
    foreach ($propertyName in @('model', 'api_base', 'api_key', 'timeout', 'stream_timeout', 'drop_params')) {
        $property = $entry.route.PSObject.Properties[$propertyName]
        if ($null -ne $property -and $null -ne $property.Value) {
            [void]$lines.Add("      ${propertyName}: $(Quote-YamlScalar $property.Value)")
        }
    }

    $allowedOpenAIParamsProperty = $entry.route.PSObject.Properties['allowed_openai_params']
    if ($null -ne $allowedOpenAIParamsProperty -and $null -ne $allowedOpenAIParamsProperty.Value) {
        $allowedOpenAIParams = @($allowedOpenAIParamsProperty.Value)
        if ($allowedOpenAIParams.Count -eq 0) {
            throw "allowed_openai_params must not be empty: $id"
        }
        [void]$lines.Add('      allowed_openai_params:')
        foreach ($allowedParam in $allowedOpenAIParams) {
            if ([string]::IsNullOrWhiteSpace([string]$allowedParam)) {
                throw "allowed_openai_params contains an empty value: $id"
            }
            [void]$lines.Add("        - $(Quote-YamlScalar $allowedParam)")
        }
    }
}

$template = [System.IO.File]::ReadAllText($TemplatePath)
$beginIndex = $template.IndexOf($BeginMarker, [StringComparison]::Ordinal)
$endIndex = $template.IndexOf($EndMarker, [StringComparison]::Ordinal)
if ($beginIndex -lt 0 -or $endIndex -lt 0 -or $endIndex -le $beginIndex) {
    throw "runtime template is missing generated model markers: $TemplatePath"
}

$newline = [Environment]::NewLine
$generatedBlock = $BeginMarker + $newline + ($lines -join $newline) + $newline + $EndMarker
$prefix = $template.Substring(0, $beginIndex)
$suffix = $template.Substring($endIndex + $EndMarker.Length)
$generated = $prefix + $generatedBlock + $suffix

$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    throw "runtime config directory is missing: $outputDirectory"
}

if ($CheckOnly) {
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
        throw "runtime config is missing: $OutputPath"
    }
    if ([System.IO.File]::ReadAllText($OutputPath) -cne $generated) {
        throw "runtime config is not generated from the current model source"
    }
    Write-Output "runtime config is current: $OutputPath"
    exit 0
}

$tempPath = "$OutputPath.tmp-$PID"
try {
    [System.IO.File]::WriteAllText($tempPath, $generated, [Text.UTF8Encoding]::new($false))
    $validator = @'
import sys
import yaml
from pathlib import Path

value = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(value, dict) or not isinstance(value.get("model_list"), list) or not value["model_list"]:
    raise SystemExit("generated runtime config has no model_list")
'@
    & $PythonPath -c $validator $tempPath
    if ($LASTEXITCODE -ne 0) {
        throw 'generated runtime config failed YAML validation'
    }
    Move-Item -LiteralPath $tempPath -Destination $OutputPath -Force
    Write-Output "runtime config generated: $OutputPath"
}
catch {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    throw
}
