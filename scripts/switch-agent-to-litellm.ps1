# 一次性切换 Codex 模型路由：默认路由 → LiteLLM(4100)。
# 保护措施：切换前备份 config.toml；失败自动回滚；全部可逆。
# 用法: pwsh -File scripts/switch-agent-to-litellm.ps1 [-Rollback]
param(
    [switch]$Rollback
)
$ErrorActionPreference = "Stop"
$Cfg = Join-Path $env:USERPROFILE ".codex\config.toml"
$Bak = Join-Path $env:USERPROFILE ".codex\config.toml.bak-agent-$(Get-Date -Format yyyyMMdd-HHmmss)"

function Get-BaseUrl {
    $py = @'
import tomllib, pathlib, os, sys
with open(pathlib.Path(os.path.expanduser("~/.codex/config.toml")), "rb") as f:
    t = tomllib.load(f)
print(t.get("openai_base_url", ""))
'@
    $py | python -
}

function Set-Route([string]$url) {
    $py = @"
import tomllib, pathlib, os
p = pathlib.Path(os.path.expanduser("~/.codex/config.toml"))
with open(p, "rb") as f:
    t = tomllib.load(f)
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
out = []
patched_url = False
patched_model = False
removed_provider = False
patched_catalog = False
for ln in lines:
    s = ln.strip()
    if s.startswith("openai_base_url"):
        indent = ln[:len(ln) - len(ln.lstrip())]
        out.append(f"{indent}openai_base_url = \"$url\"\n")
        patched_url = True
        continue
    if s.startswith("model_provider"):
        removed_provider = True
        continue
    if s.startswith("model_catalog_json"):
        indent = ln[:len(ln) - len(ln.lstrip())]
        out.append(f'{indent}model_catalog_json = "C:/Users/metra/.codex/models.json"\n')
        patched_catalog = True
        continue
    if s.startswith("model =") or s.startswith("model="):
        indent = ln[:len(ln) - len(ln.lstrip())]
        out.append(f'{indent}model = "opencode-go/deepseek-v4-flash"\n')
        patched_model = True
        continue
    out.append(ln)
if not patched_url:
    out.append(f'openai_base_url = "$url"\n')
if not patched_model:
    out.append('model = "opencode-go/deepseek-v4-flash"\n')
if not patched_catalog:
    out.append('model_catalog_json = "C:/Users/metra/.codex/models.json"\n')
p.write_text("".join(out), encoding="utf-8")
print("patched:", patched_url, patched_model, removed_provider, patched_catalog)
"@
    $py | python -
}

if ($Rollback) {
    $latest = Get-ChildItem (Join-Path $env:USERPROFILE ".codex\config.toml.bak-*") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { Write-Error "未找到备份文件，无法回滚" }
    Copy-Item $latest.FullName $Cfg -Force
    Write-Host "已回滚 config.toml <- $($latest.Name)"
    Write-Host "当前 openai_base_url = $(Get-BaseUrl)"
    exit 0
}

$before = Get-BaseUrl
Write-Host "切换前 openai_base_url = $before"
if ($before -eq "http://127.0.0.1:4100/v1") {
    Write-Host "已处于 LiteLLM 路由，无需切换"
    exit 0
}

# 1. 前置检查：LiteLLM 已运行且可达
try {
    $r = Invoke-RestMethod "http://127.0.0.1:4100/health/liveliness" -TimeoutSec 3
} catch {
    Write-Error "LiteLLM(127.0.0.1:4100) 未运行——请先执行 start-agent-gateway.ps1；未改动任何配置"
}

# 2. 备份 + 切换
Copy-Item $Cfg $Bak -Force
Write-Host "已备份 -> $Bak"
Set-Route "http://127.0.0.1:4100/v1"

# 3. 验证（HTTP 直连网关的最小请求；不依赖 codex CLI 认证状态）
$ok = $false
$throttled = $false
try {
    $req = @{
        model = "opencode-go/deepseek-v4-flash"
        input = "Reply with exactly: SWITCH_OK"
        max_output_tokens = 32
    } | ConvertTo-Json -Depth 5
    $resp = Invoke-RestMethod -Method Post `
        -Uri "http://127.0.0.1:4100/v1/responses" `
        -ContentType "application/json" `
        -Body $req `
        -TimeoutSec 90
    $ok = $true
} catch {
    # 429（上游用量上限/限流）也说明路由已打通：配置保留，仅提示。
    if ($_.Exception.Response.StatusCode.value__ -eq 429) {
        $throttled = $true
        $ok = $true
        Write-Host "警告：上游限流（HTTP 429，5 小时用量上限）；路由已打通，限流解除后可用"
    } else {
        Write-Host "网关请求失败：$($_.Exception.Message)"
    }
}
if ($ok) {
    Write-Host "切换成功：Codex 现经 LiteLLM → opencode-go 路由"
    Write-Host "回滚方法：pwsh -File scripts/switch-agent-to-litellm.ps1 -Rollback"
    exit 0
} else {
    Copy-Item $Bak $Cfg -Force
    Write-Host "验证失败——已自动回滚 config.toml"
    Write-Host "当前 openai_base_url = $(Get-BaseUrl)"
    exit 1
}
