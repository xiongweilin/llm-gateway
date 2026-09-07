# 启动协议入口：
#   Responses       -> 127.0.0.1:4100 -> LiteLLM 127.0.0.1:4101
#   Chat Completions -> 127.0.0.1:4102 -> LiteLLM 127.0.0.1:4101
#
# 用法:
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1 -NoVerify
#
# 上游凭据只从当前用户环境变量读取。

param(
    [switch]$NoVerify
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$Cfg = Join-Path $Root "litellm\config.agent.yaml"

$LogOut = Join-Path $Root "litellm\.agent-gateway.out.log"
$LogErr = Join-Path $Root "litellm\.agent-gateway.err.log"

$ResponsesProxyOut = Join-Path $Root "litellm\.responses-proxy.out.log"
$ResponsesProxyErr = Join-Path $Root "litellm\.responses-proxy.err.log"
$ChatProxyOut = Join-Path $Root "litellm\.chat-completions-proxy.out.log"
$ChatProxyErr = Join-Path $Root "litellm\.chat-completions-proxy.err.log"

$PidFile = Join-Path $Root "litellm\.agent-gateway.pid"
$ResponsesProxyPidFile = Join-Path $Root "litellm\.responses-proxy.pid"
$ChatProxyPidFile = Join-Path $Root "litellm\.chat-completions-proxy.pid"
$LegacyProxyPidFile = Join-Path $Root "litellm\.agent-proxy.pid"

$VenvPython = Join-Path $Root "litellm\.venv\Scripts\python.exe"
$ResponsesProxyScript = Join-Path $Root "tools\responses-proxy.py"
$ChatProxyScript = Join-Path $Root "tools\chat-completions-proxy.py"
$ServerEntry = Join-Path $Root "litellm\run_server.py"
$ModelSyncScript = Join-Path $Root "scripts\sync-agent-gpt-models.ps1"
$ModelCatalog = Join-Path $env:USERPROFILE ".codex\models_cache.json"
$ConfigTemplate = Join-Path $Root "litellm\config.agent.template.yaml"
$ControlPlaneBackend = if ([string]::IsNullOrWhiteSpace($env:CHATGPT_API_BASE)) {
    "https://chatgpt.com/backend-api/codex"
} else {
    $env:CHATGPT_API_BASE.TrimEnd("/")
}


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

function Get-PortOwner {
    param(
        [Parameter(Mandatory)]
        [int]$Port
    )

    return Get-NetTCPConnection `
        -LocalAddress "127.0.0.1" `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
}


function Stop-OwnPortOwner {
    param(
        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [string]$Tag
    )

    $conn = Get-PortOwner -Port $Port

    if (-not $conn) {
        return
    }

    $ownerPid = [int]$conn.OwningProcess

    $ownerProc = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$ownerPid" `
        -ErrorAction SilentlyContinue

    $cmd = ""
    if ($ownerProc) {
        $cmd = [string]$ownerProc.CommandLine
    }

    # 只清理由本项目启动的进程。
    # 未知进程继续 fail closed。
    if (
        $cmd -match 'config\.agent\.yaml' -or
        ($cmd -match 'responses-proxy\.py' -or $cmd -match 'agent-zstd-proxy\.py') -or
        $cmd -match 'chat-completions-proxy\.py' -or
        $cmd -match 'gateway[\\/]+litellm'
    ) {
        $killPid = $ownerPid
        if ($ownerProc) {
            $parentProc = Get-CimInstance Win32_Process `
                -Filter "ProcessId=$($ownerProc.ParentProcessId)" `
                -ErrorAction SilentlyContinue
            if ($parentProc -and [string]$parentProc.CommandLine -match [regex]::Escape($Root)) {
                $killPid = [int]$parentProc.ProcessId
            }
        }
        Write-Host "$Tag 端口被本桥残留进程占用，清理进程树 root=$killPid listener=$ownerPid"

        & taskkill /PID $killPid /T /F 2>$null | Out-Null

        Start-Sleep -Milliseconds 500

        $stillThere = Get-PortOwner -Port $Port

        if ($stillThere) {
            Write-Error "$Tag 无法清理 pid=$ownerPid，端口 $Port 仍被占用"
        }

        return
    }

    Write-Error "127.0.0.1:$Port 已被未知进程 $ownerPid 占用，fail closed。请先处理该进程。"
}


function Stop-BridgePort {
    param(
        [Parameter(Mandatory)]
        [int]$Port
    )

    $conn = Get-PortOwner -Port $Port

    if (-not $conn) {
        return
    }

    $ownerPid = [int]$conn.OwningProcess

    $ownerProc = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$ownerPid" `
        -ErrorAction SilentlyContinue

    $cmd = ""
    if ($ownerProc) {
        $cmd = [string]$ownerProc.CommandLine
    }

    if (
        $cmd -match 'config\.agent\.yaml' -or
        ($cmd -match 'responses-proxy\.py' -or $cmd -match 'agent-zstd-proxy\.py') -or
        $cmd -match 'chat-completions-proxy\.py' -or
        $cmd -match 'gateway[\\/]+litellm'
    ) {
        & taskkill /PID $ownerPid /T /F 2>$null | Out-Null
    }
}


function Show-LogTail {
    param(
        [string]$Title,
        [string]$Path,
        [int]$Lines = 20
    )

    Write-Host ""
    Write-Host "---- $Title ----"

    if (Test-Path $Path) {
        Get-Content $Path -Tail $Lines -ErrorAction SilentlyContinue
    }
    else {
        Write-Host "(日志不存在)"
    }
}


# LiteLLM 1.96.0 会给每个 ChatGPT Responses 请求重复注入约 7.5 KB 的旧
# Codex 提示。请求本身已经携带当前指令，只保留后端要求的最小身份前缀。
# 显式设置环境变量时仍以用户值为准。
if (-not $env:CHATGPT_DEFAULT_INSTRUCTIONS) {
    $env:CHATGPT_DEFAULT_INSTRUCTIONS = `
        "You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI on a user's computer."
}


# ------------------------------------------------------------
# 1. 基础文件检查
# ------------------------------------------------------------

if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv 不存在: $VenvPython"
}

if (-not (Test-Path $ResponsesProxyScript)) {
    Write-Error "Responses 代理脚本不存在: $ResponsesProxyScript"
}

if (-not (Test-Path $ChatProxyScript)) {
    Write-Error "Chat Completions 代理脚本不存在: $ChatProxyScript"
}

if (-not (Test-Path $ModelSyncScript)) {
    Write-Error "Codex 模型同步脚本不存在: $ModelSyncScript"
}

if (-not (Test-Path $ConfigTemplate)) {
    Write-Error "LiteLLM 配置模板不存在: $ConfigTemplate"
}

# 在停止旧网关前完成配置生成：生成失败时保留当前运行实例，避免无配置停机。
& $ModelSyncScript `
    -CatalogPath $ModelCatalog `
    -TemplatePath $ConfigTemplate `
    -OutputPath $Cfg

if (-not (Test-Path -LiteralPath $Cfg)) {
    Write-Error "模型同步完成后仍未生成 LiteLLM 配置: $Cfg"
}


# ------------------------------------------------------------
# 2. 清理本桥遗留监听
# ------------------------------------------------------------

Stop-OwnPortOwner -Port 4102 -Tag "Chat Completions 代理(4102)"
Stop-OwnPortOwner -Port 4100 -Tag "Responses 代理(4100)"
Stop-OwnPortOwner -Port 4101 -Tag "LiteLLM(4101)"

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item $ResponsesProxyPidFile -Force -ErrorAction SilentlyContinue
Remove-Item $ChatProxyPidFile -Force -ErrorAction SilentlyContinue
Remove-Item $LegacyProxyPidFile -Force -ErrorAction SilentlyContinue


# ------------------------------------------------------------
# 3. 清理旧日志
# ------------------------------------------------------------

Remove-Item $LogOut -Force -ErrorAction SilentlyContinue
Remove-Item $LogErr -Force -ErrorAction SilentlyContinue
Remove-Item $ResponsesProxyOut -Force -ErrorAction SilentlyContinue
Remove-Item $ResponsesProxyErr -Force -ErrorAction SilentlyContinue
Remove-Item $ChatProxyOut -Force -ErrorAction SilentlyContinue
Remove-Item $ChatProxyErr -Force -ErrorAction SilentlyContinue


# ------------------------------------------------------------
# 4. 启动 LiteLLM :4101
# ------------------------------------------------------------

# venv 已在前置检查中成为硬依赖，直接运行自定义入口。
# 这样 $proc 始终就是 LiteLLM Python 进程，避免包装器退出造成进程状态误判。
$proc = Start-Process `
    -FilePath $VenvPython `
    -WorkingDirectory $Root `
    -ArgumentList @(
        $ServerEntry,
        "--config", $Cfg,
        "--host", "127.0.0.1",
        "--port", "4101"
    ) `
    -RedirectStandardOutput $LogOut `
    -RedirectStandardError $LogErr `
    -WindowStyle Hidden `
    -PassThru

$proc.Id | Set-Content $PidFile

Write-Host "LiteLLM 启动命令已执行 pid=$($proc.Id)（127.0.0.1:4101）"


# ------------------------------------------------------------
# 5. 等待 LiteLLM :4101
# ------------------------------------------------------------

$ready = $false

for ($i = 0; $i -lt 90; $i++) {

    try {
        $null = Invoke-RestMethod `
            -Uri "http://127.0.0.1:4101/health/liveliness" `
            -TimeoutSec 2

        $ready = $true
        break
    }
    catch {
        Start-Sleep -Seconds 1
    }
}

if (-not $ready) {

    Show-LogTail `
        -Title "LiteLLM stderr" `
        -Path $LogErr `
        -Lines 30

    Show-LogTail `
        -Title "LiteLLM stdout" `
        -Path $LogOut `
        -Lines 20

    Stop-BridgePort -Port 4101

    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue

    Write-Error "LiteLLM 未在 90 秒内就绪"
}

$liteConn = Get-PortOwner -Port 4101

if (-not $liteConn) {
    Write-Error "LiteLLM health check 成功，但 4101 没有监听进程"
}

$litePid = $liteConn.OwningProcess

# pidfile 更新成真正监听 4101 的 PID。
$litePid | Set-Content $PidFile

Write-Host "LiteLLM 后端就绪（127.0.0.1:4101，pid=$litePid）"


# ------------------------------------------------------------
# 6. 启动 Responses 代理 :4100 -> :4101
# ------------------------------------------------------------

$proxy = Start-Process `
    -FilePath $VenvPython `
    -ArgumentList @(
        $ResponsesProxyScript,
        "4100",
        "http://127.0.0.1:4101",
        $ControlPlaneBackend
    ) `
    -RedirectStandardOutput $ResponsesProxyOut `
    -RedirectStandardError $ResponsesProxyErr `
    -WindowStyle Hidden `
    -PassThru

$proxy.Id | Set-Content $ResponsesProxyPidFile

Write-Host "Responses 代理启动命令已执行 pid=$($proxy.Id)"


# ------------------------------------------------------------
# 7. 等待 Responses 代理 :4100
# ------------------------------------------------------------

$proxyReady = $false

for ($i = 0; $i -lt 40; $i++) {

    try {
        $null = Invoke-WebRequest `
            -Uri "http://127.0.0.1:4100/health/liveliness" `
            -TimeoutSec 2 `
            -UseBasicParsing

        $proxyReady = $true
        break
    }
    catch {

        # 这里 proxy 是直接启动的 python.exe，
        # 因此 HasExited 可以可靠使用。
        if ($proxy.HasExited) {

            Show-LogTail `
                -Title "Responses 代理 stderr" `
                -Path $ResponsesProxyErr `
                -Lines 30

            Show-LogTail `
                -Title "Responses 代理 stdout" `
                -Path $ResponsesProxyOut `
                -Lines 20

            Stop-BridgePort -Port 4101

            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
            Remove-Item $ResponsesProxyPidFile -Force -ErrorAction SilentlyContinue

            Write-Error "Responses 代理启动后提前退出，exit=$($proxy.ExitCode)"
        }

        Start-Sleep -Milliseconds 500
    }
}

if (-not $proxyReady) {

    Show-LogTail `
        -Title "Responses 代理 stderr" `
        -Path $ResponsesProxyErr `
        -Lines 30

    Show-LogTail `
        -Title "Responses 代理 stdout" `
        -Path $ResponsesProxyOut `
        -Lines 20

    Stop-BridgePort -Port 4100
    Stop-BridgePort -Port 4101

    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $ResponsesProxyPidFile -Force -ErrorAction SilentlyContinue

    Write-Error "Responses 代理未在 20 秒内就绪"
}

$proxyConn = Get-PortOwner -Port 4100

if (-not $proxyConn) {
    Write-Error "Responses 代理 health check 成功，但 4100 没有监听进程"
}

$proxyPid = $proxyConn.OwningProcess

# 保存真正监听 4100 的 PID。
$proxyPid | Set-Content $ResponsesProxyPidFile

Write-Host "Responses 代理就绪（127.0.0.1:4100 -> 4101，pid=$proxyPid）"


# ------------------------------------------------------------
# 8. 启动 Chat Completions 代理 :4102 -> :4101
# ------------------------------------------------------------

$chatProxy = Start-Process `
    -FilePath $VenvPython `
    -ArgumentList @(
        $ChatProxyScript,
        "4102",
        "http://127.0.0.1:4101"
    ) `
    -RedirectStandardOutput $ChatProxyOut `
    -RedirectStandardError $ChatProxyErr `
    -WindowStyle Hidden `
    -PassThru

$chatProxy.Id | Set-Content $ChatProxyPidFile

Write-Host "Chat Completions 代理启动命令已执行 pid=$($chatProxy.Id)"

$chatProxyReady = $false

for ($i = 0; $i -lt 40; $i++) {
    try {
        $null = Invoke-WebRequest `
            -Uri "http://127.0.0.1:4102/health/liveliness" `
            -TimeoutSec 2 `
            -UseBasicParsing
        $chatProxyReady = $true
        break
    }
    catch {
        if ($chatProxy.HasExited) {
            Show-LogTail -Title "Chat Completions 代理 stderr" -Path $ChatProxyErr -Lines 30
            Show-LogTail -Title "Chat Completions 代理 stdout" -Path $ChatProxyOut -Lines 20
            Stop-BridgePort -Port 4100
            Stop-BridgePort -Port 4101
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
            Remove-Item $ResponsesProxyPidFile -Force -ErrorAction SilentlyContinue
            Remove-Item $ChatProxyPidFile -Force -ErrorAction SilentlyContinue
            Write-Error "Chat Completions 代理启动后提前退出，exit=$($chatProxy.ExitCode)"
        }
        Start-Sleep -Milliseconds 500
    }
}

if (-not $chatProxyReady) {
    Show-LogTail -Title "Chat Completions 代理 stderr" -Path $ChatProxyErr -Lines 30
    Show-LogTail -Title "Chat Completions 代理 stdout" -Path $ChatProxyOut -Lines 20
    Stop-BridgePort -Port 4102
    Stop-BridgePort -Port 4100
    Stop-BridgePort -Port 4101
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $ResponsesProxyPidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $ChatProxyPidFile -Force -ErrorAction SilentlyContinue
    Write-Error "Chat Completions 代理未在 20 秒内就绪"
}

$chatProxyConn = Get-PortOwner -Port 4102
if (-not $chatProxyConn) {
    Write-Error "Chat Completions 代理 health check 成功，但 4102 没有监听进程"
}
$chatProxyPid = $chatProxyConn.OwningProcess
$chatProxyPid | Set-Content $ChatProxyPidFile
Write-Host "Chat Completions 代理就绪（127.0.0.1:4102 -> 4101，pid=$chatProxyPid）"


# ------------------------------------------------------------
# 9. 验证协议入口 -> LiteLLM
# ------------------------------------------------------------

if (-not $NoVerify) {

    try {
        $r = Invoke-RestMethod `
            -Uri "http://127.0.0.1:4100/v1/models" `
            -TimeoutSec 30

        $chatModels = Invoke-RestMethod `
            -Uri "http://127.0.0.1:4102/v1/models" `
            -TimeoutSec 30

        Write-Host "验证通过：4100 Responses + 4102 Chat Completions -> LiteLLM -> models=$($r.data.Count)/$($chatModels.data.Count)"
    }
    catch {

        Write-Host "验证失败：$($_.Exception.Message)"

        Show-LogTail `
            -Title "LiteLLM stderr" `
            -Path $LogErr `
            -Lines 30

        Show-LogTail `
            -Title "Responses 代理 stderr" `
            -Path $ResponsesProxyErr `
            -Lines 30

        Show-LogTail `
            -Title "Chat Completions 代理 stderr" `
            -Path $ChatProxyErr `
            -Lines 30

        Stop-BridgePort -Port 4102
        Stop-BridgePort -Port 4100
        Stop-BridgePort -Port 4101

        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item $ResponsesProxyPidFile -Force -ErrorAction SilentlyContinue
        Remove-Item $ChatProxyPidFile -Force -ErrorAction SilentlyContinue

        exit 1
    }
}


# ------------------------------------------------------------
# 10. 最终状态
# ------------------------------------------------------------

Write-Host ""
Write-Host "协议入口启动完成："
Write-Host "  Responses       -> http://127.0.0.1:4100"
Write-Host "  Chat Completions -> http://127.0.0.1:4102"
Write-Host "  两个入口        -> LiteLLM http://127.0.0.1:4101"
Write-Host "  Control-plane /v1/alpha/* -> $ControlPlaneBackend"
Write-Host ""

exit 0
