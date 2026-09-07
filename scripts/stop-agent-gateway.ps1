# 停止协议入口（LiteLLM 127.0.0.1:4101 + Responses 4100 + Chat Completions 4102）。
# 按端口杀进程树（uv 包装 + litellm python 子进程），并清理 pidfile。
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidFile = Join-Path $Root "litellm\.agent-gateway.pid"
$ResponsesProxyPidFile = Join-Path $Root "litellm\.responses-proxy.pid"
$ChatProxyPidFile = Join-Path $Root "litellm\.chat-completions-proxy.pid"
$LegacyProxyPidFile = Join-Path $Root "litellm\.agent-proxy.pid"

function Stop-PortListeners {
    param([int]$Port, [string]$Tag)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    $killed = @()
    foreach ($c in $conns) {
        $owner = $c.OwningProcess
        if ($owner -and $killed -notcontains $owner) {
            & taskkill /PID $owner /T /F 2>$null | Out-Null
            Write-Host "已停止 $Tag 监听进程树 pid=$owner"
            $killed += $owner
        }
    }
    if (-not $killed) { Write-Host "$Tag：无监听进程" }
}

Stop-PortListeners -Port 4101 -Tag "LiteLLM(4101)"
Stop-PortListeners -Port 4102 -Tag "Chat Completions 代理(4102)"
Stop-PortListeners -Port 4100 -Tag "Responses 代理(4100)"

# 兜底：pidfile 中仍存活的进程（端口已释放但进程未退时）
foreach ($pf in @($PidFile, $ResponsesProxyPidFile, $ChatProxyPidFile, $LegacyProxyPidFile)) {
    if (Test-Path $pf) {
        $p = Get-Content $pf -ErrorAction SilentlyContinue
        if ($p -match '^\d+$') {
            $alive = Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue
            if ($alive) {
                & taskkill /PID ([int]$p) /T /F 2>$null | Out-Null
                Write-Host "已按 pidfile 停止残留进程 pid=$p"
            }
        }
        Remove-Item $pf -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 2
$still = @()
foreach ($p in 4100, 4101, 4102) {
    if ((Test-NetConnection 127.0.0.1 -Port $p -WarningAction SilentlyContinue).TcpTestSucceeded) { $still += $p }
}
if ($still) { Write-Host "警告：端口 $($still -join ',') 仍有监听（可能被其他程序占用，未强杀）" } else { Write-Host "网桥已全部停止" }
