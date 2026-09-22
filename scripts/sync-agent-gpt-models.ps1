# 同步客户端模型目录。
#
# 默认行为：
# - 运行时缓存和显示目录保留两个官方 GPT 模型，
#   并追加受控的 supplemental models；
# - 清理其他模型，保持固定顺序并去重；
# - 只在生成并校验完整内容后替换目标文件，失败不会破坏现有配置；
# - 本脚本不生成或决定 LiteLLM runtime configuration。

param(
    [string]$CatalogPath = (Join-Path $env:USERPROFILE ".codex\models_cache.json"),
    [string]$CodexDisplayCatalogPath = (Join-Path $env:USERPROFILE ".codex\models.json"),
    [string]$CodexConfigPath = (Join-Path $env:USERPROFILE ".codex\config.toml"),
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

# 2026-09-23：模型列表收敛为三个 —— gpt-6-sol、gpt-6-luna、
# opencode-go/deepseek-flash；顺序也是对外显示和路由顺序。
# 其余模型定义保留为注释，恢复时取消注释并加回集合。
$AllowedModelSlugs = @(
    "gpt-6-sol",
    "gpt-6-luna"
    # "gpt-5.6-sol",
    # "gpt-5.6-terra",
    # "gpt-5.6-luna"
)
$OfficialModelContextWindowOverrides = @{
    # 自 gpt-5.6-sol 沿用，待官方目录刷新后复核。
    "gpt-6-sol" = 1050000
    # "gpt-5.6-sol" = 1050000
}
$SupplementalModelDefinitions = [ordered]@{
    "opencode-go/deepseek-flash" = [ordered]@{
        DisplayName = "DeepSeek Flash (OpenCode Go)"
        Description = "OpenCode Go DeepSeek Flash model routed through the local LiteLLM gateway."
        Priority = 5
        ContextWindow = 1048576
        AutoCompactTokenLimit = 900000
        UseResponsesLite = $false
    }
    # --- 以下模型已停用（2026-09-23），恢复时取消注释 ---
    # "opencode-go/muse-spark-1.3-contributor" = [ordered]@{
    #     DisplayName = "Muse Spark 1.3 Contributor (OpenCode Go)"
    #     Description = "OpenCode Go contributor model routed through the local LiteLLM gateway."
    #     Priority = 4
    #     ContextWindow = 1048576
    #     AutoCompactTokenLimit = 900000
    #     UseResponsesLite = $false
    # }
    # "opencode-go/omen-alpha" = [ordered]@{
    #     DisplayName = "Omen Alpha (OpenCode Go)"
    #     Description = "OpenCode Go Omen Alpha model routed through the local LiteLLM gateway."
    #     Priority = 5
    #     ContextWindow = 500000
    # }
    # "opencode-go/union-alpha-free" = [ordered]@{
    #     DisplayName = "Union Alpha Free (OpenCode Go)"
    #     Description = "OpenCode Go Union Alpha Free model routed through the local LiteLLM gateway."
    #     Priority = 6
    #     # Upstream context limit has not been published in the supplied model entry.
    #     # Keep the local catalog conservative until a real protocol probe confirms it.
    #     ContextWindow = 400000
    # }
}
$SupplementalModelSlugs = @($SupplementalModelDefinitions.Keys)
$ManagedModelSlugs = @($AllowedModelSlugs + $SupplementalModelSlugs)

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

function Set-ModelProperty {
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

function Set-SupplementalCodexModelMetadata {
    param(
        [Parameter(Mandatory)]
        [object]$Object,
        [Parameter(Mandatory)]
        [string]$Slug
    )

    $definition = $SupplementalModelDefinitions[$Slug]
    if ($null -eq $definition) {
        throw "没有 OpenCode Go 模型元数据定义: $Slug"
    }

    Set-ModelProperty -Object $Object -Name "slug" -Value $Slug
    Set-ModelProperty -Object $Object -Name "display_name" -Value $definition.DisplayName
    Set-ModelProperty -Object $Object -Name "description" -Value $definition.Description
    Set-ModelProperty -Object $Object -Name "visibility" -Value "list"
    Set-ModelProperty -Object $Object -Name "supported_in_api" -Value $true
    Set-ModelProperty -Object $Object -Name "priority" -Value $definition.Priority
    Set-ModelProperty -Object $Object -Name "context_window" -Value $definition.ContextWindow
    Set-ModelProperty -Object $Object -Name "max_context_window" -Value $definition.ContextWindow
    if ($null -ne $definition.AutoCompactTokenLimit) {
        Set-ModelProperty -Object $Object -Name "auto_compact_token_limit" -Value $definition.AutoCompactTokenLimit
    }
    if ($null -ne $definition.UseResponsesLite) {
        Set-ModelProperty -Object $Object -Name "use_responses_lite" -Value $definition.UseResponsesLite
    }
    return $Object
}

function New-SupplementalCodexModel {
    param(
        [Parameter(Mandatory)]
        [object]$TemplateModel,
        [Parameter(Mandatory)]
        [string]$Slug
    )

    $model = $TemplateModel | ConvertTo-Json -Depth 100 | ConvertFrom-Json
    [void](Set-SupplementalCodexModelMetadata -Object $model -Slug $Slug)
    return $model
}

function Get-ManagedCatalogModels {
    param(
        [Parameter(Mandatory)]
        [object]$Catalog
    )

    if ($null -eq $Catalog.models) {
        throw "Codex 模型目录没有 models 数组"
    }

    $existingModels = @($Catalog.models)
    $updatedModels = [System.Collections.Generic.List[object]]::new()

    foreach ($slug in $AllowedModelSlugs) {
        $existing = @(
            $existingModels | Where-Object { [string]$_.slug -eq $slug }
        ) | Select-Object -First 1

        if ($null -eq $existing) {
            throw "Codex 目录缺少统一模型: $slug"
        }

        Set-ModelProperty -Object $existing -Name "visibility" -Value "list"
        $contextWindowOverride = $OfficialModelContextWindowOverrides[$slug]
        if ($null -ne $contextWindowOverride) {
            Set-ModelProperty -Object $existing -Name "context_window" -Value $contextWindowOverride
            Set-ModelProperty -Object $existing -Name "max_context_window" -Value $contextWindowOverride
        }
        [void]$updatedModels.Add($existing)
    }

    foreach ($supplementalSlug in $SupplementalModelSlugs) {
        $supplemental = @(
            $existingModels | Where-Object { [string]$_.slug -eq $supplementalSlug }
        ) | Select-Object -First 1

        if ($null -eq $supplemental) {
            $template = @(
                $existingModels | Where-Object { [string]$_.slug -eq "gpt-6-luna" }
            ) | Select-Object -First 1
            if ($null -eq $template) {
                throw "无法为 OpenCode Go 模型找到 Codex 元数据模板: $supplementalSlug"
            }
            $supplemental = New-SupplementalCodexModel -TemplateModel $template -Slug $supplementalSlug
        }
        else {
            [void](Set-SupplementalCodexModelMetadata -Object $supplemental -Slug $supplementalSlug)
        }

        [void]$updatedModels.Add($supplemental)
    }

    return $updatedModels.ToArray()
}

function Sync-CodexRuntimeCatalog {
    param(
        [Parameter(Mandatory)]
        [object]$Catalog
    )

    $beforeJson = $Catalog | ConvertTo-Json -Depth 100
    $Catalog.models = @(Get-ManagedCatalogModels -Catalog $Catalog)
    $afterJson = $Catalog | ConvertTo-Json -Depth 100
    $changed = $beforeJson -cne $afterJson

    if (-not $changed) {
        Write-Host "Codex 运行时模型目录无需更新"
        return $false
    }

    $catalogDirectory = Split-Path -Parent $CatalogPath
    $tempPath = "$CatalogPath.tmp-$PID"
    $backupPath = Join-Path $catalogDirectory "models_cache.json.bak-$PID"
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)

    try {
        Copy-Item -LiteralPath $CatalogPath -Destination $backupPath -Force
        $updatedJson = $Catalog | ConvertTo-Json -Depth 100
        [System.IO.File]::WriteAllText($tempPath, $updatedJson, $utf8NoBom)

        $validated = Get-Content -Raw -LiteralPath $tempPath | ConvertFrom-Json
        if (@($validated.models).Count -ne $ManagedModelSlugs.Count) {
            throw "Codex 运行时目录写入校验失败：模型数量不是 $($ManagedModelSlugs.Count)"
        }
        foreach ($slug in $ManagedModelSlugs) {
            $entry = @(
                $validated.models | Where-Object { [string]$_.slug -eq $slug }
            ) | Select-Object -First 1
            if ($null -eq $entry -or [string]$entry.visibility -ne "list") {
                throw "Codex 运行时目录写入校验失败: $slug"
            }
        }

        Move-Item -LiteralPath $tempPath -Destination $CatalogPath -Force
        Remove-Item -LiteralPath $backupPath -Force
        Write-Host "Codex 运行时模型目录已同步（保留 $($ManagedModelSlugs -join ', ')）"
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

    $existingJson = $catalog | ConvertTo-Json -Depth 100
    $catalog.models = @(Get-ManagedCatalogModels -Catalog $catalog)
    $catalogDirectory = Split-Path -Parent $CodexDisplayCatalogPath
    if (-not (Test-Path -LiteralPath $catalogDirectory)) {
        throw "Codex 显示模型目录所在目录不存在: $catalogDirectory"
    }

    $updatedJson = $catalog | ConvertTo-Json -Depth 100
    $tempPath = "$CodexDisplayCatalogPath.tmp-$PID"
    $backupPath = Join-Path $catalogDirectory "models.json.bak-$PID"
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)

    if ($existingJson -ceq $updatedJson) {
        Write-Host "Codex 显示模型目录无需更新"
        return $false
    }

    try {
        if (Test-Path -LiteralPath $CodexDisplayCatalogPath) {
            Copy-Item -LiteralPath $CodexDisplayCatalogPath -Destination $backupPath -Force
        }
        [System.IO.File]::WriteAllText($tempPath, $updatedJson, $utf8NoBom)

        $validated = Get-Content -Raw -LiteralPath $tempPath | ConvertFrom-Json
        if (@($validated.models).Count -ne $ManagedModelSlugs.Count) {
            throw "Codex 显示模型目录写入校验失败：模型数量不是 $($ManagedModelSlugs.Count)"
        }
        foreach ($slug in $ManagedModelSlugs) {
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
        Write-Host "Codex 显示模型目录已同步（保留 $($ManagedModelSlugs -join ', ')）"
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

function Get-AllowedModelSlugs {
    param(
        [object]$Catalog
    )

    if ($null -eq $Catalog.models) {
        throw "Codex 官方模型缓存没有 models 数组"
    }

    $models = [System.Collections.Generic.List[string]]::new()
    foreach ($slug in $AllowedModelSlugs) {
        if ($slug -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
            throw "统一模型 slug 无效: $slug"
        }

        $entry = @(
            $Catalog.models | Where-Object { [string]$_.slug -eq $slug }
        ) | Select-Object -First 1
        if ($null -eq $entry) {
            throw "Codex 官方模型缓存缺少统一模型: $slug"
        }

        Add-UniqueModel -Models $models -Model $slug
    }

    if ($models.Count -ne $AllowedModelSlugs.Count) {
        throw "Codex 官方模型缓存未包含完整的三模型白名单"
    }
    return $models.ToArray()
}

$catalog = Read-ModelCatalog

if ($CheckOnly) {
    $models = @(Get-AllowedModelSlugs -Catalog $catalog)
    Write-Output "Codex 模型同步预览（统一保留 $($models.Count) 个受控模型）：$($models -join ', ')"
    exit 0
}

[void](Sync-CodexRuntimeCatalog -Catalog $catalog)
[void](Sync-CodexDisplayCatalog)
[void](Ensure-CodexDisplayCatalogConfig)
Write-Host "客户端模型目录同步完成（$($ManagedModelSlugs.Count) 个受控模型）"
