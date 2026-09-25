#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Model = 'gpt-6-luna'
)

$ErrorActionPreference = 'Stop'
$restore = Join-Path $PSScriptRoot 'restore-codex-openai-gateway.ps1'
& $restore -Model $Model
exit $LASTEXITCODE
