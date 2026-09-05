# 从 Codex 官方模型缓存生成 LiteLLM model_list。
#
# 默认行为：
# - 读取 models_cache.json 中 visibility=list 的完整官方模型列表；
# - 保持官方顺序并去重，不再按 GPT 版本过滤；
# - 追加官方 Codex 模型页已公布、但本地缓存可能尚未刷新的受控模型；
# - Codex 继续使用自身未过滤的官方模型目录；
# - 只在生成完整内容后替换目标文件，失败不会破坏现有配置；
# - 运行时由 start-agent-gateway.ps1 在停止旧网关前调用。

param(
    [string]$CatalogPath = (Join-Path $env:USERPROFILE ".codex\models_cache.json"),
    [string]$TemplatePath = (Join-Path $PSScriptRoot "..\litellm\config.agent.template.yaml"),
    [string]$OutputPath = (Join-Path $PSScriptRoot "..\litellm\config.agent.yaml"),
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$BeginMarker = "  # BEGIN GENERATED GPT MODELS"
$EndMarker = "  # END GENERATED GPT MODELS"

# OpenAI 官方 Codex 模型页已经公布该模型，但 Codex 本地缓存可能滞后于文档。
# 缓存刷新后 Add-UniqueModel 会自动去重，避免重复路由。
$SupplementalOfficialModelSlugs = @(
    "gpt-6-astra"
)

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
        throw "Codex 官方模型缓存不存在；请先启动一次 Codex: $CatalogPath"
    }

    try {
        return Get-Content -Raw -LiteralPath $CatalogPath | ConvertFrom-Json
    }
    catch {
        throw "Codex 官方模型缓存无法解析: $($_.Exception.Message)"
    }
}

function Get-OfficialModelSlugs {
    param(
        [object]$Catalog
    )

    $models = [System.Collections.Generic.List[string]]::new()

    if ($null -eq $Catalog.models) {
        throw "Codex 官方模型缓存没有 models 数组"
    }

    foreach ($entry in @($Catalog.models)) {
        $slug = [string]$entry.slug

        if (
            [string]$entry.visibility -eq "list" -and
            $slug -match '^[A-Za-z0-9][A-Za-z0-9._-]*$'
        ) {
            Add-UniqueModel -Models $models -Model $slug
        }
    }

    foreach ($slug in $SupplementalOfficialModelSlugs) {
        if ($slug -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
            throw "受控补充模型 slug 无效: $slug"
        }
        Add-UniqueModel -Models $models -Model $slug
    }

    if ($models.Count -eq 0) {
        throw "Codex 官方模型缓存没有 visibility=list 的有效模型"
    }
    return $models.ToArray()
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
$models = @(Get-OfficialModelSlugs -Catalog $catalog)
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
