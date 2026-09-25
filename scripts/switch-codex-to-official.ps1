#Requires -Version 7.0

[CmdletBinding()]
param(
    [switch]$Elevated
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StopScript = Join-Path $Root "scripts\stop-agent-gateway.ps1"
$GatewayWatchScript = Join-Path $Root "scripts\watch-agent-gateway.ps1"
$GatewayWatchPattern = [regex]::Escape($GatewayWatchScript)
$ManagedGatewayTask = Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue |
    Where-Object {
        $ActionText = [string]::Join(' ', @($_.Actions | ForEach-Object { [string]$_.Execute; [string]$_.Arguments }))
        $ActionText -match $GatewayWatchPattern
    } |
    Select-Object -First 1
$GatewayTaskName = if ($null -ne $ManagedGatewayTask) {
    [string]$ManagedGatewayTask.TaskName
} else {
    "LLM-Gateway-Agent-Entry"
}
$GatewayConfigPath = Join-Path $Root "config\gateway.json"
$GatewayConfig = Get-Content -LiteralPath $GatewayConfigPath -Raw | ConvertFrom-Json
$GatewayPorts = @(
    [int]$GatewayConfig.ports.core,
    [int]$GatewayConfig.ports.agent,
    [int]$GatewayConfig.ports.responses,
    [int]$GatewayConfig.ports.chat
)
$CodexConfig = Join-Path $env:USERPROFILE ".codex\config.toml"
$BackupPath = $null

function Get-GatewayListeners {
    @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in $GatewayPorts }
    )
}

function Invoke-PwshScript {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    & $script:Pwsh -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $Path
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "子脚本失败，exit=$exitCode path=$Path"
    }
}

function Resolve-ConfigPath {
    param(
        [Parameter(Mandatory)]
        [string]$ConfiguredPath
    )

    $expanded = [Environment]::ExpandEnvironmentVariables($ConfiguredPath)
    if ([IO.Path]::IsPathRooted($expanded)) {
        return $expanded
    }
    return Join-Path (Join-Path $env:USERPROFILE ".codex") $expanded
}

$pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
$Pwsh = if ($null -ne $pwshCommand) { $pwshCommand.Source } else { Join-Path $PSHOME "pwsh.exe" }

try {
    # Preflight: all targets are exact and the existing configuration is recoverable.
    if (-not (Test-Path -LiteralPath $CodexConfig -PathType Leaf)) {
        throw "Codex 配置不存在: $CodexConfig"
    }
    if (-not (Test-Path -LiteralPath $StopScript -PathType Leaf)) {
        throw "网关停止脚本不存在: $StopScript"
    }

    $gatewayTask = Get-ScheduledTask -TaskName $GatewayTaskName -ErrorAction SilentlyContinue
    if ($null -ne $gatewayTask -and $gatewayTask.State -ne "Disabled") {
        Disable-ScheduledTask -TaskName $GatewayTaskName | Out-Null
        Write-Host "已禁用网关登录自启动: $GatewayTaskName"
    }
    elseif ($null -ne $gatewayTask) {
        Write-Host "网关登录自启动已是 Disabled"
    }
    else {
        Write-Host "未找到网关登录自启动任务，继续停止现有进程"
    }

    # This is the intentionally disruptive step. It is kept in this script so
    # the current Codex session is not interrupted by the agent itself.
    Invoke-PwshScript -Path $StopScript

    $deadline = (Get-Date).AddSeconds(15)
    do {
        $listeners = @(Get-GatewayListeners)
        if ($listeners.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    $listeners = @(Get-GatewayListeners)
    if ($listeners.Count -gt 0) {
        $ports = ($listeners | ForEach-Object { $_.LocalPort } | Sort-Object -Unique) -join ","
        throw "网关端口仍在监听: $ports"
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupPath = "$CodexConfig.bak-official-$stamp"
    Copy-Item -LiteralPath $CodexConfig -Destination $BackupPath -Force
    if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
        throw "Codex 配置备份失败: $BackupPath"
    }
    Write-Host "Codex 配置已备份: $BackupPath"

    $before = [System.IO.File]::ReadAllText($CodexConfig)
    # Remove only active top-level/custom endpoint settings. Comments and all
    # other official Codex settings remain unchanged.
    $after = [regex]::Replace(
        $before,
        '(?m)^[ \t]*openai_base_url[ \t]*=[^\r\n]*(?:\r\n|\n|$)',
        ""
    )

    if ($after -ne $before) {
        [System.IO.File]::WriteAllText(
            $CodexConfig,
            $after,
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Host "已移除自定义 openai_base_url，Codex 将使用官方默认路由"
    }
    else {
        Write-Host "未发现活动的 openai_base_url，配置已经是官方路由"
    }

    $current = [System.IO.File]::ReadAllText($CodexConfig)
    if ($current -match '(?m)^[ \t]*openai_base_url[ \t]*=') {
        throw "配置仍包含活动的 openai_base_url"
    }

    # Do not regenerate or replace a model catalog here. Model visibility is
    # owned by the official Codex configuration and the settings it references.
    $catalogMatch = [regex]::Match(
        $current,
        '(?m)^[ \t]*model_catalog_json[ \t]*=[ \t]*"([^"]+)"'
    )
    if ($catalogMatch.Success) {
        $catalogPath = Resolve-ConfigPath -ConfiguredPath $catalogMatch.Groups[1].Value
        Write-Host "保留 Codex 官方模型显示配置: model_catalog_json=$catalogPath"
        if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) {
            Write-Warning "model_catalog_json 指向的文件不存在，Codex 可能回退到内置模型目录: $catalogPath"
        }
    }
    else {
        Write-Warning "未发现 model_catalog_json；Codex 将使用内置官方模型目录"
    }

    $modelMatch = [regex]::Match($current, '(?m)^[ \t]*model[ \t]*=[ \t]*"([^"]+)"')
    if ($modelMatch.Success) {
        Write-Host "Codex 默认模型保持: $($modelMatch.Groups[1].Value)"
    }

    $finalTask = Get-ScheduledTask -TaskName $GatewayTaskName -ErrorAction SilentlyContinue
    if ($null -ne $finalTask -and $finalTask.State -ne "Disabled") {
        throw "网关计划任务未处于 Disabled: $GatewayTaskName state=$($finalTask.State)"
    }

    Write-Host "完成：LLM Gateway 已停止，Codex 配置已切换为官方路由。"
    Write-Host "请完全退出并重新打开 Codex Desktop/CLI；不要在当前会话中强杀 codex.exe。"
    exit 0
}
catch {
    $message = $_.Exception.Message
    if (-not $Elevated -and $message -match "(?i)access denied|unauthorized|拒绝访问|权限") {
        Write-Warning "检测到权限不足，弹出 UAC 后以管理员身份重试。"
        $args = @(
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$PSCommandPath`"",
            "-Elevated"
        )
        $child = Start-Process -FilePath $Pwsh -Verb RunAs -ArgumentList $args -Wait -PassThru
        exit $child.ExitCode
    }

    Write-Error "切换失败: $message"
    if ($null -ne $BackupPath) {
        Write-Host "配置备份仍保留: $BackupPath"
        Write-Host "回滚配置: Copy-Item -LiteralPath `"$BackupPath`" -Destination `"$CodexConfig`" -Force"
    }
    Write-Host "若需恢复网关: Enable-ScheduledTask -TaskName `"$GatewayTaskName`"; pwsh -NoProfile -File `"$Root\scripts\start-agent-gateway.ps1`""
    exit 1
}
