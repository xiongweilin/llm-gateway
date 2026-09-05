# 启动 Codex 模型路由桥接：
#   Codex -> zstd 解压代理(127.0.0.1:4100)
#         -> LiteLLM(127.0.0.1:4101)
#         -> ChatGPT GPT family
#
# 用法:
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1 -NoVerify
#
# 当前 GPT-only 路由复用 Codex 登录态，本脚本不读取 provider API key。

param(
    [switch]$NoVerify
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$Cfg = Join-Path $Root "litellm\config.agent.yaml"

$LogOut = Join-Path $Root "litellm\.agent-gateway.out.log"
$LogErr = Join-Path $Root "litellm\.agent-gateway.err.log"

$ProxyOut = Join-Path $Root "litellm\.agent-proxy.out.log"
$ProxyErr = Join-Path $Root "litellm\.agent-proxy.err.log"

$PidFile = Join-Path $Root "litellm\.agent-gateway.pid"
$ProxyPidFile = Join-Path $Root "litellm\.agent-proxy.pid"

$VenvPython = Join-Path $Root "litellm\.venv\Scripts\python.exe"
$ProxyScript = Join-Path $Root "tools\agent-zstd-proxy.py"
$ServerEntry = Join-Path $Root "litellm\run_server.py"
$ModelSyncScript = Join-Path $Root "scripts\sync-agent-gpt-models.ps1"
$ModelCatalog = Join-Path $env:USERPROFILE ".codex\models_cache.json"
$ConfigTemplate = Join-Path $Root "litellm\config.agent.template.yaml"


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
        $cmd -match 'agent-zstd-proxy\.py' -or
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
        $cmd -match 'agent-zstd-proxy\.py' -or
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

if (-not (Test-Path $ProxyScript)) {
    Write-Error "zstd proxy 脚本不存在: $ProxyScript"
}

if (-not (Test-Path $ModelSyncScript)) {
    Write-Error "GPT 模型同步脚本不存在: $ModelSyncScript"
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

Stop-OwnPortOwner -Port 4100 -Tag "zstd 代理(4100)"
Stop-OwnPortOwner -Port 4101 -Tag "LiteLLM(4101)"

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item $ProxyPidFile -Force -ErrorAction SilentlyContinue


# ------------------------------------------------------------
# 3. 清理旧日志
# ------------------------------------------------------------

Remove-Item $LogOut -Force -ErrorAction SilentlyContinue
Remove-Item $LogErr -Force -ErrorAction SilentlyContinue
Remove-Item $ProxyOut -Force -ErrorAction SilentlyContinue
Remove-Item $ProxyErr -Force -ErrorAction SilentlyContinue


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

Write-Host "LiteLLM codex 网关就绪（127.0.0.1:4101，pid=$litePid）"


# ------------------------------------------------------------
# 6. 启动 zstd proxy :4100 -> :4101
# ------------------------------------------------------------

$proxy = Start-Process `
    -FilePath $VenvPython `
    -ArgumentList @(
        $ProxyScript,
        "4100",
        "http://127.0.0.1:4101"
    ) `
    -RedirectStandardOutput $ProxyOut `
    -RedirectStandardError $ProxyErr `
    -WindowStyle Hidden `
    -PassThru

$proxy.Id | Set-Content $ProxyPidFile

Write-Host "zstd 代理启动命令已执行 pid=$($proxy.Id)"


# ------------------------------------------------------------
# 7. 等待 zstd proxy :4100
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
                -Title "zstd proxy stderr" `
                -Path $ProxyErr `
                -Lines 30

            Show-LogTail `
                -Title "zstd proxy stdout" `
                -Path $ProxyOut `
                -Lines 20

            Stop-BridgePort -Port 4101

            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
            Remove-Item $ProxyPidFile -Force -ErrorAction SilentlyContinue

            Write-Error "zstd 代理启动后提前退出，exit=$($proxy.ExitCode)"
        }

        Start-Sleep -Milliseconds 500
    }
}

if (-not $proxyReady) {

    Show-LogTail `
        -Title "zstd proxy stderr" `
        -Path $ProxyErr `
        -Lines 30

    Show-LogTail `
        -Title "zstd proxy stdout" `
        -Path $ProxyOut `
        -Lines 20

    Stop-BridgePort -Port 4100
    Stop-BridgePort -Port 4101

    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $ProxyPidFile -Force -ErrorAction SilentlyContinue

    Write-Error "zstd 代理未在 20 秒内就绪"
}

$proxyConn = Get-PortOwner -Port 4100

if (-not $proxyConn) {
    Write-Error "zstd proxy health check 成功，但 4100 没有监听进程"
}

$proxyPid = $proxyConn.OwningProcess

# 保存真正监听 4100 的 PID。
$proxyPid | Set-Content $ProxyPidFile

Write-Host "zstd 解压代理就绪（127.0.0.1:4100 -> 4101，pid=$proxyPid）"


# ------------------------------------------------------------
# 8. 验证 :4100 -> :4101
# ------------------------------------------------------------

if (-not $NoVerify) {

    try {
        $r = Invoke-RestMethod `
            -Uri "http://127.0.0.1:4100/v1/models" `
            -TimeoutSec 30

        Write-Host "验证通过：代理 -> LiteLLM -> GPT models=$($r.data.Count)"
    }
    catch {

        Write-Host "验证失败：$($_.Exception.Message)"

        Show-LogTail `
            -Title "LiteLLM stderr" `
            -Path $LogErr `
            -Lines 30

        Show-LogTail `
            -Title "zstd proxy stderr" `
            -Path $ProxyErr `
            -Lines 30

        Stop-BridgePort -Port 4100
        Stop-BridgePort -Port 4101

        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item $ProxyPidFile -Force -ErrorAction SilentlyContinue

        exit 1
    }
}


# ------------------------------------------------------------
# 9. 最终状态
# ------------------------------------------------------------

Write-Host ""
Write-Host "Codex 网桥启动完成："
Write-Host "  Codex      -> zstd proxy http://127.0.0.1:4100"
Write-Host "  zstd proxy -> LiteLLM http://127.0.0.1:4101"
Write-Host "  LiteLLM    -> ChatGPT GPT family"
Write-Host ""

exit 0
