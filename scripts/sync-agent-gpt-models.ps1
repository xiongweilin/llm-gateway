# 同步 Codex 模型目录与 Gateway GPT 路由。
#
# 默认行为：
# - 从当前 Codex 二进制附带的模型目录发现最新 sol/luna；
# - Codex 运行时缓存、显示目录和 Gateway 路由只保留各系列最新版本；
# - 追加受控的 supplemental models；
# - 清理其他模型，保持固定顺序并去重；
# - 只在生成并校验完整内容后替换目标文件，失败不会破坏现有配置；
# - 路由变化后重启 Gateway，使旧模型 ID 立即不可调用。

param(
    [string]$CatalogPath = (Join-Path $env:USERPROFILE ".codex\models_cache.json"),
    [string]$CodexDisplayCatalogPath = (Join-Path $env:USERPROFILE ".codex\models.json"),
    [string]$GatewayConfigPath = (Join-Path $PSScriptRoot "..\config\gateway.json"),
    [string]$GatewayModelsPath,
    [string]$GatewayStartScript = (Join-Path $PSScriptRoot "start-agent-gateway.ps1"),
    [switch]$CheckOnly,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"

# Model IDs are discovered from the installed Codex bundled catalog at run time.
$AllowedModelSlugs = @()
$OfficialModelContextWindowOverrides = @{
    # Retained only for the exact model this historical override names.
    "gpt-6-sol" = 1050000
}
$SupplementalModelDefinitions = [ordered]@{
    "opencode-go/deepseek-flash" = [ordered]@{
        DisplayName = "DeepSeek Flash (OpenCode Go)"
        Description = "OpenCode Go DeepSeek Flash model routed through the local LLM Gateway."
        Priority = 5
        ContextWindow = 1048576
        AutoCompactTokenLimit = 900000
        UseResponsesLite = $false
    }
    "sonnet-5.5" = [ordered]@{
        DisplayName = "Claude Sonnet 5.5"
        Description = "Anthropic Claude Sonnet 5.5 routed through the local LLM Gateway."
        Priority = 5
        ContextWindow = 1000000
        AutoCompactTokenLimit = 900000
        MaxOutputTokens = 128000
        DefaultReasoningLevel = "high"
        SupportedReasoningLevels = @(
            [ordered]@{ effort = "low"; description = "Lower latency and lighter reasoning" }
            [ordered]@{ effort = "medium"; description = "Balances latency and reasoning depth" }
            [ordered]@{ effort = "high"; description = "Deeper reasoning for complex tasks" }
            [ordered]@{ effort = "xhigh"; description = "Extra-high reasoning depth" }
            [ordered]@{ effort = "max"; description = "Maximum reasoning depth" }
        )
        InputModalities = @("text", "image")
        SupportsSearchTool = $false
        SupportsParallelToolCalls = $true
        UseResponsesLite = $false
    }
    # --- 以下模型已停用（2026-09-23），恢复时取消注释 ---
    # "opencode-go/muse-spark-1.3-contributor" = [ordered]@{
    #     DisplayName = "Muse Spark 1.3 Contributor (OpenCode Go)"
    #     Description = "OpenCode Go contributor model routed through the local LLM Gateway."
    #     Priority = 4
    #     ContextWindow = 1048576
    #     AutoCompactTokenLimit = 900000
    #     UseResponsesLite = $false
    # }
    # "opencode-go/omen-alpha" = [ordered]@{
    #     DisplayName = "Omen Alpha (OpenCode Go)"
    #     Description = "OpenCode Go Omen Alpha model routed through the local LLM Gateway."
    #     Priority = 5
    #     ContextWindow = 500000
    # }
    # "opencode-go/union-alpha-free" = [ordered]@{
    #     DisplayName = "Union Alpha Free (OpenCode Go)"
    #     Description = "OpenCode Go Union Alpha Free model routed through the local LLM Gateway."
    #     Priority = 6
    #     # 已提供的 model entry 中尚未公布上游 context limit。
    #     # 在真实 protocol probe 确认前，保持本地 catalog 保守。
    #     ContextWindow = 400000
    # }
}
$SupplementalModelSlugs = @($SupplementalModelDefinitions.Keys)
$ManagedModelSlugs = @()

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
    $codexCommand = Get-Command codex.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $raw = & $codexCommand.Source debug models --bundled 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "读取 Codex bundled model catalog 失败（exit=$LASTEXITCODE）：$raw"
    }
    try {
        $catalog = $raw | ConvertFrom-Json
    }
    catch {
        throw "Codex bundled model catalog 无法解析: $($_.Exception.Message)"
    }
    if ($null -eq $catalog.models -or $catalog.models -isnot [array]) {
        throw "Codex bundled model catalog 没有 models 数组"
    }
    return $catalog
}

function Compare-ModelVersion {
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )

    $leftParts = @($Left -split '\.' | ForEach-Object { [int]::Parse($_) })
    $rightParts = @($Right -split '\.' | ForEach-Object { [int]::Parse($_) })
    $partCount = [Math]::Max($leftParts.Count, $rightParts.Count)
    for ($index = 0; $index -lt $partCount; $index++) {
        $leftPart = if ($index -lt $leftParts.Count) { $leftParts[$index] } else { 0 }
        $rightPart = if ($index -lt $rightParts.Count) { $rightParts[$index] } else { 0 }
        if ($leftPart -gt $rightPart) { return 1 }
        if ($leftPart -lt $rightPart) { return -1 }
    }
    return 0
}

function Get-LatestOfficialModelSlugs {
    param([Parameter(Mandatory)][object]$Catalog)

    $latest = @{}
    foreach ($model in $Catalog.models) {
        $match = [regex]::Match(
            [string]$model.slug,
            '^gpt-(?<version>\d+(?:\.\d+)*?)-(?<family>sol|luna)$'
        )
        if (-not $match.Success) { continue }
        if ([string]$model.visibility -cne "list" -or $model.supported_in_api -ne $true) {
            continue
        }

        $family = $match.Groups["family"].Value
        $version = $match.Groups["version"].Value
        if (-not $latest.ContainsKey($family)) {
            $latest[$family] = [pscustomobject]@{
                Slug = [string]$model.slug
                Version = $version
            }
            continue
        }

        $comparison = Compare-ModelVersion -Left $version -Right $latest[$family].Version
        if ($comparison -gt 0) {
            $latest[$family] = [pscustomobject]@{
                Slug = [string]$model.slug
                Version = $version
            }
        }
        elseif ($comparison -eq 0 -and [string]$model.slug -cne $latest[$family].Slug) {
            throw "Codex catalog has ambiguous $family model IDs at version $version"
        }
    }

    foreach ($family in @("sol", "luna")) {
        if (-not $latest.ContainsKey($family)) {
            throw "Codex bundled model catalog has no visible, API-supported gpt-$family model"
        }
    }
    return @($latest["sol"].Slug, $latest["luna"].Slug)
}

function Set-ModelProperty {
    param(
        [Parameter(Mandatory)]
        [object]$Object,
        [Parameter(Mandatory)]
        [string]$Name,
        [Parameter(Mandatory)]
        [AllowNull()]
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
        throw "没有补充模型元数据定义: $Slug"
    }

    Set-ModelProperty -Object $Object -Name "slug" -Value $Slug
    Set-ModelProperty -Object $Object -Name "display_name" -Value $definition.DisplayName
    Set-ModelProperty -Object $Object -Name "description" -Value $definition.Description
    Set-ModelProperty -Object $Object -Name "visibility" -Value "list"
    Set-ModelProperty -Object $Object -Name "supported_in_api" -Value $true
    Set-ModelProperty -Object $Object -Name "priority" -Value $definition.Priority
    Set-ModelProperty -Object $Object -Name "context_window" -Value $definition.ContextWindow
    Set-ModelProperty -Object $Object -Name "max_context_window" -Value $definition.ContextWindow
    if ($null -ne $definition.MaxOutputTokens) {
        Set-ModelProperty -Object $Object -Name "max_output_tokens" -Value $definition.MaxOutputTokens
    }
    if ($null -ne $definition.DefaultReasoningLevel) {
        Set-ModelProperty -Object $Object -Name "default_reasoning_level" -Value $definition.DefaultReasoningLevel
    }
    if ($null -ne $definition.SupportedReasoningLevels) {
        Set-ModelProperty -Object $Object -Name "supported_reasoning_levels" -Value @($definition.SupportedReasoningLevels)
    }
    if ($null -ne $definition.InputModalities) {
        Set-ModelProperty -Object $Object -Name "input_modalities" -Value @($definition.InputModalities)
    }
    if ($null -ne $definition.SupportsSearchTool) {
        Set-ModelProperty -Object $Object -Name "supports_search_tool" -Value $definition.SupportsSearchTool
    }
    if ($null -ne $definition.SupportsParallelToolCalls) {
        Set-ModelProperty -Object $Object -Name "supports_parallel_tool_calls" -Value $definition.SupportsParallelToolCalls
    }
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
            $templateSlug = @(
                $AllowedModelSlugs | Where-Object { $_ -match '-luna$' }
            ) | Select-Object -First 1
            $template = @(
                $existingModels | Where-Object { [string]$_.slug -eq $templateSlug }
            ) | Select-Object -First 1
            if ($null -eq $template) {
                throw "无法为补充模型找到 Codex 元数据模板: $supplementalSlug"
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

function Get-ManagedDisplayCatalogModels {
    param(
        [Parameter(Mandatory)][object]$SourceCatalog,
        [Parameter(Mandatory)][object]$ExistingCatalog
    )

    if ($null -eq $ExistingCatalog.models) {
        throw "Codex 显示模型目录没有 models 数组"
    }
    $existingModels = @($ExistingCatalog.models)
    $updatedModels = [System.Collections.Generic.List[object]]::new()

    foreach ($slug in $AllowedModelSlugs) {
        $sourceModel = @(
            $SourceCatalog.models | Where-Object { [string]$_.slug -ceq $slug }
        ) | Select-Object -First 1
        if ($null -eq $sourceModel) {
            throw "Codex bundled model catalog 缺少最新模型: $slug"
        }
        $displayModel = @(
            $existingModels | Where-Object { [string]$_.slug -ceq $slug }
        ) | Select-Object -First 1
        if ($null -eq $displayModel) {
            $family = if ($slug -match '-sol$') { 'sol' } else { 'luna' }
            $displayModel = @(
                $existingModels | Where-Object {
                    [string]$_.slug -match "^gpt-\d+(?:\.\d+)*-$family$"
                }
            ) | Select-Object -First 1
        }
        if ($null -eq $displayModel) {
            throw "无法为 Codex 显示模型创建 $slug 的目录元数据模板"
        }

        $displayModel = $displayModel | ConvertTo-Json -Depth 100 | ConvertFrom-Json
        foreach ($property in $sourceModel.PSObject.Properties) {
            Set-ModelProperty -Object $displayModel -Name $property.Name -Value $property.Value
        }
        Set-ModelProperty -Object $displayModel -Name "slug" -Value $slug
        Set-ModelProperty -Object $displayModel -Name "visibility" -Value "list"
        Set-ModelProperty -Object $displayModel -Name "supported_in_api" -Value $true
        $contextWindowOverride = $OfficialModelContextWindowOverrides[$slug]
        if ($null -ne $contextWindowOverride) {
            Set-ModelProperty -Object $displayModel -Name "context_window" -Value $contextWindowOverride
            Set-ModelProperty -Object $displayModel -Name "max_context_window" -Value $contextWindowOverride
        }
        if ($null -eq $displayModel.PSObject.Properties['supports_parallel_tool_calls']) {
            throw "Codex 显示目录模板缺少必需字段 supports_parallel_tool_calls: $slug"
        }
        [void]$updatedModels.Add($displayModel)
    }

    foreach ($supplementalSlug in $SupplementalModelSlugs) {
        $supplemental = @(
            $existingModels | Where-Object { [string]$_.slug -ceq $supplementalSlug }
        ) | Select-Object -First 1
        if ($null -eq $supplemental) {
            $templateSlug = @(
                $AllowedModelSlugs | Where-Object { $_ -match '-luna$' }
            ) | Select-Object -First 1
            $template = @(
                $updatedModels | Where-Object { [string]$_.slug -ceq $templateSlug }
            ) | Select-Object -First 1
            if ($null -eq $template) {
                throw "无法为补充模型找到 Codex 显示目录模板: $supplementalSlug"
            }
            $supplemental = New-SupplementalCodexModel -TemplateModel $template -Slug $supplementalSlug
        }
        else {
            $supplemental = $supplemental | ConvertTo-Json -Depth 100 | ConvertFrom-Json
            [void](Set-SupplementalCodexModelMetadata -Object $supplemental -Slug $supplementalSlug)
        }
        if ($null -eq $supplemental.PSObject.Properties['supports_parallel_tool_calls']) {
            throw "Codex 显示目录模板缺少必需字段 supports_parallel_tool_calls: $supplementalSlug"
        }
        [void]$updatedModels.Add($supplemental)
    }

    return $updatedModels.ToArray()
}

function Sync-CodexDisplayCatalog {
    param([Parameter(Mandatory)][object]$SourceCatalog)

    if (Test-Path -LiteralPath $CodexDisplayCatalogPath -PathType Leaf) {
        try {
            $catalog = Get-Content -Raw -LiteralPath $CodexDisplayCatalogPath | ConvertFrom-Json
        }
        catch {
            throw "Codex 显示模型目录无法解析: $($_.Exception.Message)"
        }
    }
    else {
        $catalog = $SourceCatalog | ConvertTo-Json -Depth 100 | ConvertFrom-Json
    }
    if ($null -eq $catalog.models) {
        throw "Codex 显示模型目录没有 models 数组"
    }

    $existingJson = $catalog | ConvertTo-Json -Depth 100
    $catalog.models = @(Get-ManagedDisplayCatalogModels -SourceCatalog $SourceCatalog -ExistingCatalog $catalog)
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

function Get-GatewayModelsPlan {
    param(
        [Parameter(Mandatory)][string]$Content,
        [Parameter(Mandatory)][string[]]$LatestSlugs
    )

    $lineEnding = if ($Content.Contains(([string][char]13 + [char]10))) {
        ([string][char]13 + [char]10)
    } else {
        [string][char]10
    }
    $hasFinalNewline = $Content.EndsWith([char]10)
    $lines = [regex]::Split($Content, '\r?\n')
    $starts = [System.Collections.Generic.List[int]]::new()
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -cmatch '^\s{2}- id:\s*\S+\s*$') {
            $starts.Add($index)
        }
    }
    if ($starts.Count -eq 0) {
        throw "Gateway 模型配置中没有 models 路由项"
    }

    $blocks = [System.Collections.Generic.List[object]]::new()
    for ($blockIndex = 0; $blockIndex -lt $starts.Count; $blockIndex++) {
        $start = $starts[$blockIndex]
        $end = if ($blockIndex + 1 -lt $starts.Count) {
            $starts[$blockIndex + 1] - 1
        } else {
            $lines.Count - 1
        }
        $blockLines = @($lines[$start..$end])
        $idMatch = [regex]::Match($blockLines[0], '^\s{2}- id:\s*(?<id>\S+)\s*$')
        if (-not $idMatch.Success) { throw "无法解析 Gateway 模型路由行: $($blockLines[0])" }
        $familyMatch = [regex]::Match(
            $idMatch.Groups["id"].Value,
            '^gpt-(?<version>\d+(?:\.\d+)*)-(?<family>sol|luna)$'
        )
        $family = if ($familyMatch.Success) { $familyMatch.Groups["family"].Value } else { $null }
        $blocks.Add([pscustomobject]@{
            Id = $idMatch.Groups["id"].Value
            Family = $family
            Lines = $blockLines
            UpdatedLines = $null
        })
    }

    $latestByFamily = @{
        sol = ($LatestSlugs | Where-Object { $_ -match '-sol$' } | Select-Object -First 1)
        luna = ($LatestSlugs | Where-Object { $_ -match '-luna$' } | Select-Object -First 1)
    }
    $selectedIndexByFamily = @{}
    $retiredIds = [System.Collections.Generic.List[string]]::new()
    foreach ($family in @("sol", "luna")) {
        $candidates = @(
            for ($index = 0; $index -lt $blocks.Count; $index++) {
                if ($blocks[$index].Family -ceq $family) {
                    [pscustomobject]@{ Index = $index; Block = $blocks[$index] }
                }
            }
        )
        if ($candidates.Count -eq 0) {
            throw "Gateway route config has no existing gpt-$family route to migrate"
        }
        $matchingLatest = @($candidates | Where-Object { $_.Block.Id -ceq $latestByFamily[$family] })
        $selected = if ($matchingLatest.Count -gt 0) { $matchingLatest[0] } else { $candidates[0] }
        $selectedIndexByFamily[$family] = [int]$selected.Index
        if ($selected.Block.Id -cne $latestByFamily[$family]) {
            $retiredIds.Add($selected.Block.Id)
        }

        $updatedLines = [System.Collections.Generic.List[string]]::new()
        foreach ($line in $selected.Block.Lines) { $updatedLines.Add($line) }
        $idLine = [regex]::Match($updatedLines[0], '^(?<prefix>\s{2}- id:\s*).+$')
        if (-not $idLine.Success) { throw "Cannot update gpt-$family route ID" }
        $updatedLines[0] = $idLine.Groups["prefix"].Value + $latestByFamily[$family]

        $upstreamIndex = -1
        for ($lineIndex = 0; $lineIndex -lt $updatedLines.Count; $lineIndex++) {
            if ($updatedLines[$lineIndex] -cmatch '^\s+upstream_model:\s*') {
                $upstreamIndex = $lineIndex
                break
            }
        }
        if ($upstreamIndex -lt 0) {
            throw "Gateway gpt-$family route has no upstream_model field"
        }
        $upstreamLine = [regex]::Match(
            $updatedLines[$upstreamIndex],
            '^(?<prefix>\s+upstream_model:\s*).+$'
        )
        if (-not $upstreamLine.Success) { throw "Cannot update gpt-$family upstream model" }
        $updatedLines[$upstreamIndex] = $upstreamLine.Groups["prefix"].Value + $latestByFamily[$family]

        $routeText = $updatedLines -join ([string][char]10)
        if (
            $routeText -notmatch '(?m)^\s+mode:\s*responses\s*$' -or
            $routeText -notmatch '(?m)^\s+authorization:\s*chatgpt\s*$' -or
            $routeText -notmatch '(?m)^\s+api_base:\s*https://chatgpt\.com/backend-api/codex\s*$'
        ) {
            throw "Gateway gpt-$family route does not match the ChatGPT Codex Responses contract"
        }
        $selected.Block.UpdatedLines = $updatedLines.ToArray()

        foreach ($candidate in $candidates) {
            if ($candidate.Index -ne $selected.Index) {
                $retiredIds.Add($candidate.Block.Id)
            }
        }
    }

    $outputLines = [System.Collections.Generic.List[string]]::new()
    $firstStart = $starts[0]
    for ($index = 0; $index -lt $firstStart; $index++) { $outputLines.Add($lines[$index]) }
    for ($index = 0; $index -lt $blocks.Count; $index++) {
        $block = $blocks[$index]
        if ($null -eq $block.Family) {
            foreach ($line in $block.Lines) { $outputLines.Add($line) }
            continue
        }
        if ($selectedIndexByFamily[$block.Family] -ne $index) { continue }
        foreach ($line in $block.UpdatedLines) { $outputLines.Add($line) }
    }

    $newContent = $outputLines -join $lineEnding
    if ($hasFinalNewline -and -not $newContent.EndsWith([char]10)) {
        $newContent += $lineEnding
    }
    $routeIds = [System.Collections.Generic.List[string]]::new()
    for ($index = 0; $index -lt $blocks.Count; $index++) {
        $block = $blocks[$index]
        if ($null -eq $block.Family) {
            $routeIds.Add($block.Id)
        }
        elseif ($selectedIndexByFamily[$block.Family] -eq $index) {
            $routeIds.Add($latestByFamily[$block.Family])
        }
    }
    [pscustomobject]@{
        Content = $newContent
        Changed = $newContent -cne $Content
        CurrentGptIds = @($blocks | Where-Object { $null -ne $_.Family } | ForEach-Object { $_.Id })
        LatestIds = @($latestByFamily.sol, $latestByFamily.luna)
        RetiredIds = $retiredIds.ToArray()
        RouteIds = $routeIds.ToArray()
    }
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Content
    )

    $directory = Split-Path -Parent $Path
    $temporaryPath = Join-Path $directory ((Split-Path -Leaf $Path) + ".tmp-$PID")
    try {
        [System.IO.File]::WriteAllText($temporaryPath, $Content, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-AllowedModelSlugs {
    param(
        [object]$Catalog
    )

    if ($null -eq $Catalog.models) {
        throw "Codex bundled model catalog 没有 models 数组"
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
            throw "Codex bundled model catalog 缺少最新模型: $slug"
        }

        Add-UniqueModel -Models $models -Model $slug
    }

    if ($models.Count -ne $AllowedModelSlugs.Count) {
        throw "Codex bundled model catalog 未包含完整的 sol/luna 模型集合"
    }
    return $models.ToArray()
}

$gatewayRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gatewayConfig = Get-Content -LiteralPath $GatewayConfigPath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($GatewayModelsPath)) {
    $GatewayModelsPath = Join-Path $gatewayRoot ([string]$gatewayConfig.models_file)
}
else {
    $GatewayModelsPath = [System.IO.Path]::GetFullPath($GatewayModelsPath)
}
$GatewayStartScript = [System.IO.Path]::GetFullPath($GatewayStartScript)
foreach ($requiredPath in @($GatewayModelsPath, $GatewayStartScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Gateway model update path is missing: $requiredPath"
    }
}

$catalog = Read-ModelCatalog
$AllowedModelSlugs = @(Get-LatestOfficialModelSlugs -Catalog $catalog)
$ManagedModelSlugs = @($AllowedModelSlugs + $SupplementalModelSlugs)
$models = @(Get-AllowedModelSlugs -Catalog $catalog)
$currentGatewayContent = [System.IO.File]::ReadAllText($GatewayModelsPath)
$gatewayPlan = Get-GatewayModelsPlan -Content $currentGatewayContent -LatestSlugs $AllowedModelSlugs

if ($CheckOnly) {
    if (-not (Test-Path -LiteralPath $CodexDisplayCatalogPath -PathType Leaf)) {
        throw "Codex 显示模型目录不存在: $CodexDisplayCatalogPath"
    }
    $existingDisplayCatalog = Get-Content -Raw -LiteralPath $CodexDisplayCatalogPath | ConvertFrom-Json
    $displayPreview = @(Get-ManagedDisplayCatalogModels -SourceCatalog $catalog -ExistingCatalog $existingDisplayCatalog)
    Write-Output "Codex bundled latest models: $($models -join ', ')"
    Write-Output "Current Gateway GPT routes: $($gatewayPlan.CurrentGptIds -join ', ')"
    if ($gatewayPlan.RetiredIds.Count -gt 0) {
        Write-Output "Will retire (not callable after apply): $($gatewayPlan.RetiredIds -join ', ')"
    }
    if ($gatewayPlan.Changed) {
        Write-Output "Will update Gateway routes to: $($gatewayPlan.LatestIds -join ', ')"
    }
    else {
        Write-Output "Gateway GPT routes already match the latest bundled catalog"
    }
    Write-Output "Codex display catalog validation passed for $($displayPreview.Count) managed models"
    Write-Output "Managed Codex model catalog after apply: $($ManagedModelSlugs -join ', ')"
    exit 0
}

$trackedPaths = @($CatalogPath, $CodexDisplayCatalogPath, $GatewayModelsPath)
$originalContents = @{}
foreach ($path in $trackedPaths) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $originalContents[$path] = [System.IO.File]::ReadAllText($path)
    }
    else {
        $originalContents[$path] = $null
    }
}

$runtimeCatalog = $catalog | ConvertTo-Json -Depth 100 | ConvertFrom-Json
$displayCatalog = $catalog | ConvertTo-Json -Depth 100 | ConvertFrom-Json
try {
    $runtimeChanged = Sync-CodexRuntimeCatalog -Catalog $runtimeCatalog
    $displayChanged = Sync-CodexDisplayCatalog -SourceCatalog $displayCatalog
    if ($gatewayPlan.Changed) {
        Write-AtomicText -Path $GatewayModelsPath -Content $gatewayPlan.Content
        $gatewayChanged = $true
        Write-Host "Gateway routes updated: $($gatewayPlan.LatestIds -join ', ')"
    }
    else {
        $gatewayChanged = $false
        Write-Host "Gateway GPT routes already match the bundled catalog"
    }
}
catch {
    foreach ($path in $trackedPaths) {
        $original = $originalContents[$path]
        if ($null -eq $original) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
        elseif ((Test-Path -LiteralPath $path -PathType Leaf) -and
            [System.IO.File]::ReadAllText($path) -cne $original) {
            Write-AtomicText -Path $path -Content $original
        }
    }
    throw
}

$restartGateway = $gatewayChanged
if (-not $restartGateway) {
    $agentUri = "http://$($gatewayConfig.listen_host):$($gatewayConfig.ports.agent)/v1/models"
    try {
        $liveCatalog = Invoke-RestMethod -Uri $agentUri -TimeoutSec 3
        $liveIds = @($liveCatalog.data | ForEach-Object { [string]$_.id } | Sort-Object -Unique)
        $expectedIds = @($gatewayPlan.RouteIds | Sort-Object -Unique)
        $liveJoined = [string]::Join([char]10, $liveIds)
        $expectedJoined = [string]::Join([char]10, $expectedIds)
        $restartGateway = $liveJoined -cne $expectedJoined
    }
    catch {
        $restartGateway = $true
    }
}

if ($restartGateway -and -not $NoRestart) {
    Write-Warning "Restarting Gateway services to apply the model routes; finish active model requests before running this script."
    & $GatewayStartScript
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Gateway restart failed with exit code $LASTEXITCODE; updated model files remain in place"
    }
    Write-Host "Gateway restarted; its startup checks validated the protocol catalogs"
}
elseif ($restartGateway) {
    Write-Warning "Gateway files are updated; restart is required before the new route set is active."
}
else {
    Write-Host "Gateway runtime catalog already matches the configured routes"
}

Write-Host "Model synchronization completed: $($ManagedModelSlugs -join ', ')"
