# 从 Codex 模型目录生成 LiteLLM 的 GPT-only model_list。
#
# 默认行为：
# - 始终保留 gpt-5.6-sol 与 gpt-5.6-luna；
# - 读取 models.json 中所有 GPT-5.6 及更新系列模型并去重追加；
# - 同时生成 LiteLLM 配置和 Codex 使用的 models.filtered.json；
# - 只在生成完整内容后替换目标文件，失败不会破坏现有配置；
# - 运行时由 start-agent-gateway.ps1 在停止旧网关前调用。

param(
    [string]$CatalogPath = (Join-Path $env:USERPROFILE ".codex\models.json"),
    [string]$CodexCatalogOutputPath = (Join-Path $env:USERPROFILE ".codex\models.filtered.json"),
    [string]$TemplatePath = (Join-Path $PSScriptRoot "..\litellm\config.agent.template.yaml"),
    [string]$OutputPath = (Join-Path $PSScriptRoot "..\litellm\config.agent.yaml"),
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$BeginMarker = "  # BEGIN GENERATED GPT MODELS"
$EndMarker = "  # END GENERATED GPT MODELS"
$BaselineModels = @("gpt-5.6-sol", "gpt-5.6-luna")

function Add-UniqueModel {
    param(
        [System.Collections.Generic.List[string]]$Models,
        [Parameter(Mandatory)]
        [string]$Model
    )

    if ($Models -notcontains $Model) {
        [void]$Models.Add($Model)
    }
}

function Read-ModelCatalog {
    if (-not (Test-Path -LiteralPath $CatalogPath)) {
        Write-Warning "Codex 模型目录不存在，使用 GPT 基线模型: $CatalogPath"
        return $null
    }

    try {
        return Get-Content -Raw -LiteralPath $CatalogPath | ConvertFrom-Json
    }
    catch {
        Write-Warning "Codex 模型目录无法解析，使用 GPT 基线模型: $($_.Exception.Message)"
        return $null
    }
}

function Test-IsCurrentOrNewerGptModel {
    param(
        [Parameter(Mandatory)]
        [string]$Slug
    )

    if ($Slug -notmatch '^gpt-([0-9]+)(?:\.([0-9]+))?(?:[.-]|$)[A-Za-z0-9._-]*$') {
        return $false
    }

    $major = [int]$matches[1]
    $minor = if ($matches[2]) { [int]$matches[2] } else { 0 }
    return $major -gt 5 -or ($major -eq 5 -and $minor -ge 6)
}

function Set-ObjectProperty {
    param(
        [Parameter(Mandatory)]
        [object]$Object,
        [Parameter(Mandatory)]
        [string]$Name,
        [Parameter(Mandatory)]
        [object]$Value
    )

    if ($null -ne $Object.PSObject.Properties[$Name]) {
        $Object.$Name = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Get-GptModelSlugs {
    param(
        [object]$Catalog
    )

    $models = [System.Collections.Generic.List[string]]::new()

    foreach ($baseline in $BaselineModels) {
        Add-UniqueModel -Models $models -Model $baseline
    }

    if ($null -eq $Catalog) {
        return $models.ToArray()
    }

    if ($null -eq $Catalog.models) {
        Write-Warning "Codex 模型目录没有 models 数组，使用 GPT 基线模型"
        return $models.ToArray()
    }

    foreach ($entry in @($Catalog.models)) {
        $slug = [string]$entry.slug

        # 只接受 GPT-5.6 及更新系列，避免把旧 GPT、gpt-oss 或非模型字段加入路由。
        # slug 只允许模型名常见字符，防止目录内容注入 YAML。
        if (
            (Test-IsCurrentOrNewerGptModel -Slug $slug) -and
            $slug -notmatch '["''`\r\n]'
        ) {
            Add-UniqueModel -Models $models -Model $slug
        }
    }

    return $models.ToArray()
}

function Get-FilteredCodexModels {
    param(
        [Parameter(Mandatory)]
        [object]$Catalog
    )

    $selected = [System.Collections.Generic.List[object]]::new()
    $sourceEntries = @($Catalog.models)
    $luna = $sourceEntries | Where-Object { [string]$_.slug -eq "gpt-5.6-luna" } | Select-Object -First 1
    $sol = $sourceEntries | Where-Object { [string]$_.slug -eq "gpt-5.6-sol" } | Select-Object -First 1

    if ($null -ne $sol) {
        [void]$selected.Add($sol)
    }
    elseif ($null -ne $luna) {
        # 当前 Codex 目录尚未提供 Sol 条目；以 Luna 的完整 schema 为基线，
        # 只覆盖已由 OpenAI 文档确认的 Sol 标识和上下文窗口字段。
        $sol = $luna | ConvertTo-Json -Depth 100 | ConvertFrom-Json
        Set-ObjectProperty -Object $sol -Name slug -Value "gpt-5.6-sol"
        Set-ObjectProperty -Object $sol -Name display_name -Value "GPT-5.6-Sol (OpenAI 账号)"
        Set-ObjectProperty -Object $sol -Name description -Value "Routed via ChatGPT account (OpenAI balance)."
        Set-ObjectProperty -Object $sol -Name context_window -Value 1050000
        Set-ObjectProperty -Object $sol -Name max_context_window -Value 1050000
        Set-ObjectProperty -Object $sol -Name auto_compact_token_limit -Value 945000
        [void]$selected.Add($sol)
    }

    if ($null -ne $luna) {
        [void]$selected.Add($luna)
    }

    foreach ($entry in $sourceEntries) {
        $slug = [string]$entry.slug
        if ((Test-IsCurrentOrNewerGptModel -Slug $slug) -and ($selected | Where-Object { $_.slug -eq $slug }).Count -eq 0) {
            [void]$selected.Add($entry)
        }
    }

    return $selected.ToArray()
}

function New-GeneratedModelBlock {
    param(
        [Parameter(Mandatory)]
        [string[]]$Models
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($model in $Models) {
        # 模型 slug 已经通过严格字符白名单校验，这里用单引号保持 YAML 标量稳定。
        [void]$lines.Add("  - model_name: '$model'")
        [void]$lines.Add("    model_info:")
        [void]$lines.Add("      mode: responses")
        [void]$lines.Add("    litellm_params:")
        [void]$lines.Add("      model: 'chatgpt/$model'")
        [void]$lines.Add("      timeout: 120")
    }
    return $lines.ToArray()
}

if (-not (Test-Path -LiteralPath $TemplatePath)) {
    throw "LiteLLM 配置模板不存在: $TemplatePath"
}

$template = Get-Content -Raw -LiteralPath $TemplatePath
$beginIndex = $template.IndexOf($BeginMarker, [System.StringComparison]::Ordinal)
$endIndex = $template.IndexOf($EndMarker, [System.StringComparison]::Ordinal)

if ($beginIndex -lt 0 -or $endIndex -lt 0 -or $endIndex -le $beginIndex) {
    throw "配置模板缺少有效的 GPT 模型标记"
}

$catalog = Read-ModelCatalog
$models = @(Get-GptModelSlugs -Catalog $catalog)
$block = (New-GeneratedModelBlock -Models $models) -join [Environment]::NewLine
$replacement = "$BeginMarker$([Environment]::NewLine)$block$([Environment]::NewLine)$EndMarker"

$prefix = $template.Substring(0, $beginIndex)
$suffixStart = $endIndex + $EndMarker.Length
$suffix = $template.Substring($suffixStart)
$generated = $prefix + $replacement + $suffix

if ($CheckOnly) {
    Write-Output "GPT 模型同步预览（$($models.Count) 个）：$($models -join ', ')"
    exit 0
}

$filteredCatalogModels = @()
if ($null -ne $catalog -and $null -ne $catalog.models) {
    $filteredCatalogModels = @(Get-FilteredCodexModels -Catalog $catalog)
}

$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    throw "LiteLLM 配置目录不存在: $outputDirectory"
}

$tempPath = "$OutputPath.tmp-$PID"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

try {
    [System.IO.File]::WriteAllText($tempPath, $generated, $utf8NoBom)

    $same = $false
    if (Test-Path -LiteralPath $OutputPath) {
        $existing = [System.IO.File]::ReadAllText($OutputPath)
        $same = $existing -ceq $generated
    }

    if ($same) {
        Remove-Item -LiteralPath $tempPath -Force
        Write-Host "GPT 模型配置无需更新（$($models.Count) 个）"
    }
    else {
        Move-Item -LiteralPath $tempPath -Destination $OutputPath -Force
        Write-Host "GPT 模型配置已同步（$($models.Count) 个）：$($models -join ', ')"
    }
}
catch {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    throw
}

if ($filteredCatalogModels.Count -gt 0) {
    $catalogDirectory = Split-Path -Parent $CodexCatalogOutputPath
    if (-not (Test-Path -LiteralPath $catalogDirectory)) {
        throw "Codex 模型目录输出目录不存在: $catalogDirectory"
    }

    $filteredCatalog = $catalog | ConvertTo-Json -Depth 100 | ConvertFrom-Json
    $filteredCatalog.models = @($filteredCatalogModels)
    $filteredJson = $filteredCatalog | ConvertTo-Json -Depth 100
    $catalogTempPath = "$CodexCatalogOutputPath.tmp-$PID"

    try {
        [System.IO.File]::WriteAllText($catalogTempPath, $filteredJson, $utf8NoBom)
        $sameCatalog = $false
        if (Test-Path -LiteralPath $CodexCatalogOutputPath) {
            $existingCatalog = [System.IO.File]::ReadAllText($CodexCatalogOutputPath)
            $sameCatalog = $existingCatalog -ceq $filteredJson
        }

        if ($sameCatalog) {
            Remove-Item -LiteralPath $catalogTempPath -Force
            Write-Host "Codex GPT 模型目录无需更新（$($filteredCatalogModels.Count) 个）"
        }
        else {
            Move-Item -LiteralPath $catalogTempPath -Destination $CodexCatalogOutputPath -Force
            Write-Host "Codex GPT 模型目录已同步（$($filteredCatalogModels.Count) 个）：$($filteredCatalogModels.slug -join ', ')"
        }
    }
    catch {
        Remove-Item -LiteralPath $catalogTempPath -Force -ErrorAction SilentlyContinue
        throw
    }
}
else {
    Write-Warning "未生成 Codex 过滤目录；保留现有文件: $CodexCatalogOutputPath"
}
