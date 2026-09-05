# 从 Codex 官方模型缓存生成 LiteLLM model_list。
#
# 默认行为：
# - 读取 models_cache.json 中 visibility=list 的完整官方模型列表；
# - 保持官方顺序并去重，不再按 GPT 版本过滤；
# - 追加官方 Codex 模型页已公布、但本地缓存可能尚未刷新的受控模型；
# - 将受控补充模型幂等合并回 Codex 运行时目录，避免 Codex 列表落后于官方文档；
# - 只在生成完整内容后替换目标文件，失败不会破坏现有配置；
# - 运行时由 start-agent-gateway.ps1 在停止旧网关前调用。

param(
    [string]$CatalogPath = (Join-Path $env:USERPROFILE ".codex\models_cache.json"),
    [string]$CodexDisplayCatalogPath = (Join-Path $env:USERPROFILE ".codex\models.json"),
    [string]$CodexConfigPath = (Join-Path $env:USERPROFILE ".codex\config.toml"),
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

function New-SupplementalCodexModel {
    param(
        [Parameter(Mandatory)]
        [object]$TemplateModel,
        [Parameter(Mandatory)]
        [string]$Slug
    )

    $model = $TemplateModel | ConvertTo-Json -Depth 100 | ConvertFrom-Json

    switch ($Slug) {
        "gpt-6-astra" {
            $model.slug = "gpt-6-astra"
            $model.display_name = "GPT-6 Astra"
            $model.description = "Our most capable model for complex work across code, apps, and research, combining advanced reasoning, computer use, and stronger judgment."
            $model.default_reasoning_level = "low"
            $model.supported_reasoning_levels = @(
                [pscustomobject]@{ effort = "low"; description = "Fast responses with lighter reasoning" },
                [pscustomobject]@{ effort = "medium"; description = "Balances speed and reasoning depth for everyday tasks" },
                [pscustomobject]@{ effort = "high"; description = "Greater reasoning depth for complex problems" },
                [pscustomobject]@{ effort = "xhigh"; description = "Extra high reasoning depth for complex problems" },
                [pscustomobject]@{ effort = "max"; description = "Maximum reasoning depth for the hardest problems" }
            )
            $model.visibility = "list"
            $model.supported_in_api = $true
            $model.priority = 0
            $model.context_window = 1050000
            $model.max_context_window = 1050000
        }
        default {
            throw "没有受控补充模型元数据: $Slug"
        }
    }

    return $model
}

function Sync-CodexRuntimeCatalog {
    param(
        [Parameter(Mandatory)]
        [object]$Catalog
    )

    $existingModels = @($Catalog.models)
    $supplementalSet = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($slug in $SupplementalOfficialModelSlugs) {
        [void]$supplementalSet.Add($slug)
    }

    $updatedModels = [System.Collections.Generic.List[object]]::new()
    $changed = $false

    foreach ($slug in $SupplementalOfficialModelSlugs) {
        $existing = @(
            $existingModels | Where-Object { [string]$_.slug -eq $slug }
        ) | Select-Object -First 1

        if ($null -eq $existing) {
            $templateModel = @(
                $existingModels | Where-Object { [string]$_.visibility -eq "list" }
            ) | Select-Object -First 1
            if ($null -eq $templateModel) {
                throw "无法为 Codex 补充模型找到可复制的 visibility=list 元数据模板"
            }

            [void]$updatedModels.Add(
                (New-SupplementalCodexModel -TemplateModel $templateModel -Slug $slug)
            )
            $changed = $true
            Write-Host "Codex 运行时目录将补充官方模型: $slug"
        }
        else {
            [void]$updatedModels.Add($existing)
            if ([string]$existing.visibility -ne "list") {
                $existing.visibility = "list"
                $changed = $true
                Write-Host "Codex 运行时目录将显示官方模型: $slug"
            }
        }
    }

    foreach ($model in $existingModels) {
        if (-not $supplementalSet.Contains([string]$model.slug)) {
            [void]$updatedModels.Add($model)
        }
    }

    if (-not $changed) {
        Write-Host "Codex 运行时模型目录无需更新"
        return $false
    }

    $Catalog.models = $updatedModels.ToArray()
    $catalogDirectory = Split-Path -Parent $CatalogPath
    $tempPath = "$CatalogPath.tmp-$PID"
    $backupPath = Join-Path $catalogDirectory "models_cache.json.bak-$PID"
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)

    try {
        Copy-Item -LiteralPath $CatalogPath -Destination $backupPath -Force
        $updatedJson = $Catalog | ConvertTo-Json -Depth 100
        [System.IO.File]::WriteAllText($tempPath, $updatedJson, $utf8NoBom)

        $validated = Get-Content -Raw -LiteralPath $tempPath | ConvertFrom-Json
        foreach ($slug in $SupplementalOfficialModelSlugs) {
            $entry = @(
                $validated.models | Where-Object { [string]$_.slug -eq $slug }
            ) | Select-Object -First 1
            if ($null -eq $entry -or [string]$entry.visibility -ne "list") {
                throw "Codex 运行时目录写入校验失败: $slug"
            }
        }

        Move-Item -LiteralPath $tempPath -Destination $CatalogPath -Force
        Remove-Item -LiteralPath $backupPath -Force
        Write-Host "Codex 运行时模型目录已同步（$($SupplementalOfficialModelSlugs -join ', ')）"
        return $true
    }
    catch {
        if (Test-Path -LiteralPath $backupPath) {
            Copy-Item -LiteralPath $backupPath -Destination $CatalogPath -Force
        }
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Read-CodexDisplayCatalog {
    if (Test-Path -LiteralPath $CodexDisplayCatalogPath) {
        try {
            return Get-Content -Raw -LiteralPath $CodexDisplayCatalogPath | ConvertFrom-Json
        }
        catch {
            throw "Codex 显示模型目录无法解析: $($_.Exception.Message)"
        }
    }

    $codexCommand = Get-Command codex -ErrorAction Stop
    $raw = & $codexCommand.Source debug models 2>$null | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取 Codex 当前模型目录，codex debug models 退出码: $LASTEXITCODE"
    }

    try {
        return $raw | ConvertFrom-Json
    }
    catch {
        throw "Codex 模型目录输出无法解析: $($_.Exception.Message)"
    }
}

function Sync-CodexDisplayCatalog {
    $catalog = Read-CodexDisplayCatalog
    if ($null -eq $catalog.models) {
        throw "Codex 显示模型目录没有 models 数组"
    }

    $existingModels = @($catalog.models)
    $supplementalSet = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($slug in $SupplementalOfficialModelSlugs) {
        [void]$supplementalSet.Add($slug)
    }

    $updatedModels = [System.Collections.Generic.List[object]]::new()
    $changed = $false

    foreach ($slug in $SupplementalOfficialModelSlugs) {
        $existing = @(
            $existingModels | Where-Object { [string]$_.slug -eq $slug }
        ) | Select-Object -First 1

        if ($null -eq $existing) {
            $templateModel = @(
                $existingModels | Where-Object { [string]$_.visibility -eq "list" }
            ) | Select-Object -First 1
            if ($null -eq $templateModel) {
                throw "无法为 Codex 显示目录找到可复制的 visibility=list 元数据模板"
            }

            [void]$updatedModels.Add(
                (New-SupplementalCodexModel -TemplateModel $templateModel -Slug $slug)
            )
            $changed = $true
            Write-Host "Codex 显示目录将补充官方模型: $slug"
        }
        else {
            [void]$updatedModels.Add($existing)
            if ([string]$existing.visibility -ne "list") {
                $existing.visibility = "list"
                $changed = $true
                Write-Host "Codex 显示目录将显示官方模型: $slug"
            }
        }
    }

    foreach ($model in $existingModels) {
        if (-not $supplementalSet.Contains([string]$model.slug)) {
            [void]$updatedModels.Add($model)
        }
    }

    $catalog.models = $updatedModels.ToArray()
    $catalogDirectory = Split-Path -Parent $CodexDisplayCatalogPath
    if (-not (Test-Path -LiteralPath $catalogDirectory)) {
        throw "Codex 显示模型目录所在目录不存在: $catalogDirectory"
    }

    $updatedJson = $catalog | ConvertTo-Json -Depth 100
    $tempPath = "$CodexDisplayCatalogPath.tmp-$PID"
    $backupPath = Join-Path $catalogDirectory "models.json.bak-$PID"
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)

    $existingJson = $null
    if (Test-Path -LiteralPath $CodexDisplayCatalogPath) {
        $existingJson = [System.IO.File]::ReadAllText($CodexDisplayCatalogPath)
    }

    if ($null -ne $existingJson -and $existingJson -ceq $updatedJson) {
        Write-Host "Codex 显示模型目录无需更新"
        return $false
    }

    try {
        if (Test-Path -LiteralPath $CodexDisplayCatalogPath) {
            Copy-Item -LiteralPath $CodexDisplayCatalogPath -Destination $backupPath -Force
        }
        [System.IO.File]::WriteAllText($tempPath, $updatedJson, $utf8NoBom)

        $validated = Get-Content -Raw -LiteralPath $tempPath | ConvertFrom-Json
        foreach ($slug in $SupplementalOfficialModelSlugs) {
            $entry = @(
                $validated.models | Where-Object { [string]$_.slug -eq $slug }
            ) | Select-Object -First 1
            if (
                $null -eq $entry -or
                [string]$entry.visibility -ne "list" -or
                $null -eq $entry.PSObject.Properties['supports_parallel_tool_calls']
            ) {
                throw "Codex 显示模型目录写入校验失败: $slug"
            }
        }

        Move-Item -LiteralPath $tempPath -Destination $CodexDisplayCatalogPath -Force
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force
        }
        Write-Host "Codex 显示模型目录已同步（$($SupplementalOfficialModelSlugs -join ', ')）"
        return $true
    }
    catch {
        if (Test-Path -LiteralPath $backupPath) {
            Copy-Item -LiteralPath $backupPath -Destination $CodexDisplayCatalogPath -Force
        }
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Ensure-CodexDisplayCatalogConfig {
    if (-not (Test-Path -LiteralPath $CodexConfigPath)) {
        throw "Codex 配置不存在: $CodexConfigPath"
    }

    $configuredPath = $CodexDisplayCatalogPath.Replace("\", "/")
    $setting = 'model_catalog_json = "' + $configuredPath + '"'
    $lines = [System.IO.File]::ReadAllLines($CodexConfigPath)
    $updatedLines = [System.Collections.Generic.List[string]]::new()
    $found = $false

    foreach ($line in $lines) {
        if ($line -match '^\s*model_catalog_json\s*=') {
            if (-not $found) {
                $indent = $line.Substring(0, $line.Length - $line.TrimStart().Length)
                [void]$updatedLines.Add("$indent$setting")
                $found = $true
            }
        }
        else {
            [void]$updatedLines.Add($line)
            if (-not $found -and $line -match '^\s*openai_base_url\s*=') {
                [void]$updatedLines.Add($setting)
                $found = $true
            }
        }
    }

    if (-not $found) {
        [void]$updatedLines.Add($setting)
    }

    $updatedConfig = ($updatedLines -join [Environment]::NewLine) + [Environment]::NewLine
    $existingConfig = [System.IO.File]::ReadAllText($CodexConfigPath)
    if ($existingConfig -ceq $updatedConfig) {
        Write-Host "Codex 配置已引用显示模型目录"
        return $false
    }

    $configDirectory = Split-Path -Parent $CodexConfigPath
    $tempPath = "$CodexConfigPath.tmp-$PID"
    $backupPath = Join-Path $configDirectory "config.toml.bak-model-catalog-$PID"
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)

    try {
        Copy-Item -LiteralPath $CodexConfigPath -Destination $backupPath -Force
        [System.IO.File]::WriteAllText($tempPath, $updatedConfig, $utf8NoBom)
        Move-Item -LiteralPath $tempPath -Destination $CodexConfigPath -Force
        Remove-Item -LiteralPath $backupPath -Force
        Write-Host "Codex 配置已引用: $CodexDisplayCatalogPath"
        return $true
    }
    catch {
        if (Test-Path -LiteralPath $backupPath) {
            Copy-Item -LiteralPath $backupPath -Destination $CodexConfigPath -Force
        }
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        throw
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

[void](Sync-CodexRuntimeCatalog -Catalog $catalog)
[void](Sync-CodexDisplayCatalog)
[void](Ensure-CodexDisplayCatalogConfig)

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
