# 启动 Codex 模型路由桥接：
#   Codex -> zstd 解压代理(127.0.0.1:4100)
#         -> LiteLLM(127.0.0.1:4101)
#         -> opencode-go
#
# 用法:
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1 -NoVerify
#   pwsh -NoProfile -File scripts\start-agent-gateway.ps1 -KeyEnv <path>
#
# 密钥来源优先级:
#   $env:OPENCODEGO_API_KEY
#   $env:USERPROFILE\.codex\litellm-opencode-go.env
#
# 本脚本不会打印密钥值。

param(
    [string]$KeyEnv = "",
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
$VenvLiteLLM = Join-Path $Root "litellm\.venv\Scripts\litellm.exe"
$ProxyScript = Join-Path $Root "tools\agent-zstd-proxy.py"
$ServerEntry = Join-Path $Root "litellm\run_server.py"


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
        Write-Host "$Tag 端口被本桥残留进程占用，清理 pid=$ownerPid"

        & taskkill /PID $ownerPid /T /F 2>$null | Out-Null

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


# ------------------------------------------------------------
# 1. 解析 API Key
# ------------------------------------------------------------

if (-not $env:OPENCODEGO_API_KEY) {

    if (-not $KeyEnv) {
        $KeyEnv = Join-Path `
            $env:USERPROFILE `
            ".codex\litellm-opencode-go.env"
    }

    if (Test-Path $KeyEnv) {

        foreach ($line in Get-Content $KeyEnv) {

            if (
                $line -match '^\s*OPENCODEGO_API_KEY\s*=\s*(.+?)\s*$'
            ) {
                $env:OPENCODEGO_API_KEY = $matches[1]
                break
            }
        }
    }
}

if (-not $env:OPENCODEGO_API_KEY) {
    Write-Error @"
未找到 OPENCODEGO_API_KEY。

请：
1. 设置 `$env:OPENCODEGO_API_KEY
或
2. 写入：
   $env:USERPROFILE\.codex\litellm-opencode-go.env
"@
}

# DeepSeek 官方 API key（可选；DeepSeek 路由需要）。
if (-not $env:DEEPSEEK_API_KEY) {
    $DeepSeekKeyEnv = Join-Path `
        $env:USERPROFILE `
        ".codex\litellm-deepseek.env"
    if (Test-Path $DeepSeekKeyEnv) {
        foreach ($line in Get-Content $DeepSeekKeyEnv) {
            if ($line -match '^\s*DEEPSEEK_API_KEY\s*=\s*(.+?)\s*$') {
                $env:DEEPSEEK_API_KEY = $matches[1]
                break
            }
        }
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
# 2. 基础文件检查
# ------------------------------------------------------------

if (-not (Test-Path $Cfg)) {
    Write-Error "LiteLLM 配置不存在: $Cfg"
}

if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv 不存在: $VenvPython"
}

if (-not (Test-Path $ProxyScript)) {
    Write-Error "zstd proxy 脚本不存在: $ProxyScript"
}


# ------------------------------------------------------------
# 3. 清理本桥遗留监听
# ------------------------------------------------------------

Stop-OwnPortOwner -Port 4100 -Tag "zstd 代理(4100)"
Stop-OwnPortOwner -Port 4101 -Tag "LiteLLM(4101)"

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item $ProxyPidFile -Force -ErrorAction SilentlyContinue


# ------------------------------------------------------------
# 4. 清理旧日志
# ------------------------------------------------------------

Remove-Item $LogOut -Force -ErrorAction SilentlyContinue
Remove-Item $LogErr -Force -ErrorAction SilentlyContinue
Remove-Item $ProxyOut -Force -ErrorAction SilentlyContinue
Remove-Item $ProxyErr -Force -ErrorAction SilentlyContinue


# ------------------------------------------------------------
# 5. 启动 LiteLLM :4101
# ------------------------------------------------------------

Push-Location $Root

try {

    if (Test-Path $VenvPython) {

        # 首选 venv python 直接运行 litellm 入口（run_server.py）。
        # 不用 venv 中的 litellm.exe：那是 uv 生成的 trampoline，项目目录
        # 移动后无法 canonicalize 脚本路径（"uv trampoline failed to
        # canonicalize script path"）。这样 $proc 就是 LiteLLM 进程本身，
        # 不经过 cmd.exe，避免父进程退出造成误判。
        $proc = Start-Process `
            -FilePath $VenvPython `
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
    }
    else {

        # venv 中没有 litellm.exe 时才 fallback 到 uv。
        # 后面的 readiness 判断以真实端口为准，
        # 不依赖 uv wrapper 是否仍然存活。
        $proc = Start-Process `
            -FilePath "uv" `
            -ArgumentList @(
                "run",
                "--no-sync",
                "--project", "litellm",
                "litellm",
                "--config", $Cfg,
                "--host", "127.0.0.1",
                "--port", "4101"
            ) `
            -RedirectStandardOutput $LogOut `
            -RedirectStandardError $LogErr `
            -WindowStyle Hidden `
            -PassThru
    }
}
finally {
    Pop-Location
}

$proc.Id | Set-Content $PidFile

Write-Host "LiteLLM 启动命令已执行 pid=$($proc.Id)（127.0.0.1:4101）"


# ------------------------------------------------------------
# 6. 等待 LiteLLM :4101
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
# 7. 启动 zstd proxy :4100 -> :4101
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
# 8. 等待 zstd proxy :4100
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
# 9. 验证 :4100 -> :4101
# ------------------------------------------------------------

if (-not $NoVerify) {

    try {
        $r = Invoke-RestMethod `
            -Uri "http://127.0.0.1:4100/v1/models" `
            -TimeoutSec 30

        Write-Host "验证通过：代理 -> LiteLLM -> opencode-go models=$($r.data.Count)"
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
# 10. 最终状态
# ------------------------------------------------------------

Write-Host ""
Write-Host "Codex 网桥启动完成："
Write-Host "  Codex      -> http://127.0.0.1:4100"
Write-Host "  zstd proxy -> http://127.0.0.1:4101"
Write-Host "  LiteLLM    -> opencode-go"
Write-Host ""

exit 0
