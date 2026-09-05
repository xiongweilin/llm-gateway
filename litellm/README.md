# litellm-gateway —— 项目内独立 LiteLLM 模型网关

> 为个人 Agent 项目提供独立的 LiteLLM 模型网关组件。Codex 当前使用
> `config.agent.yaml` 路由。

## 特性

- 独立 uv 环境：`uv.lock` 锁定全部依赖，LiteLLM 固定 `==1.96.0`。
- 无数据库运行：不设置 `database_url`，`store_model_in_db: false`，配置只来自 `config.yaml`。
- 仅绑定 loopback（默认 `127.0.0.1:4100`），master key 由环境变量注入，配置无明文密钥。
- 关闭 response cache / semantic cache / 外部日志 callbacks，不记录请求/响应正文。
- 自带 fake provider 与 conformance 测试（消息顺序、稳定前缀、cache usage、streaming、取消、认证、绑定）。

## 环境要求

- Windows + PowerShell；uv ≥ 0.12（本机 0.12.3）。
- 本机 Python 3.14.6 可用（项目 `requires-python = ">=3.11"`，无需降级；若需 3.12：`uv python install 3.12` 后删除 `.python-version` 中版本或改为 `3.12`）。

## 安装

```powershell
cd D:\agent\litellm-gateway\litellm
uv sync --locked
```

首次同步 `litellm[proxy]` 依赖较大，可能耗时数分钟，属正常。

锁定说明（详见 [CONFORMANCE.md](CONFORMANCE.md) 第 5 节）：

- `litellm[proxy]==1.96.0`（精确锁定）。
- `fastapi>=0.136.0,<0.137.0`：litellm 1.96.0 仍依赖 fastapi 0.137 已移除的 `get_flat_dependant`，uv 默认解析的最新 fastapi 会让代理启动崩溃，故钉住 0.136.x。
- `prisma==0.15.0`：litellm 1.96.0 认证异常处理器在 DB-less 下无条件 import prisma，缺失时认证失败会从 401 变 500；安装后恢复 401（仍不连接数据库）。

## 配置

Conformance 环境的配置事实源是 [config.yaml](config.yaml)；Codex 路由的配置事实源是
[config.agent.yaml](config.agent.yaml)。运行 conformance 前需注入环境变量：

```powershell
$env:LITELLM_MASTER_KEY   = "sk-你的-master-key"        # 网关 master key（必填）
$env:FAKE_PROVIDER_API_BASE = "http://127.0.0.1:15000/v1"  # conformance fake provider 地址
$env:FAKE_PROVIDER_API_KEY  = "sk-fake-canary-5678"        # conformance 合成 key
```

真实 provider（deepseek/openai）模板已注释在 config.yaml 中，启用时取消注释并注入对应环境变量（如 `$env:DEEPSEEK_API_KEY`），**不要在 config.yaml 写明文 key**。

## 启动网关

```powershell
uv run litellm --config config.yaml --host 127.0.0.1 --port 4100
```

说明：

- `--host 127.0.0.1` 是标准运行方式（仅本机访问）。litellm 1.96.0 的 CLI 默认 host 是 `0.0.0.0`，切勿在未加 `--host 127.0.0.1` 时暴露到网络。
- 健康检查：`http://127.0.0.1:4100/health/liveliness`。
- 调用示例（模型名可用 `fake-responses` 或别名 `fake-gw`）：

```powershell
curl.exe http://127.0.0.1:4100/v1/responses `
  -H "Authorization: Bearer $env:LITELLM_MASTER_KEY" `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"fake-responses\",\"input\":\"hello\"}'
```

## 运行 conformance 测试

```powershell
uv run pytest -v
```

测试套件会：启动 fake provider（OpenAI 兼容 `/v1/responses` 与 `/v1/chat/completions`）→ 用临时端口启动 litellm 代理子进程（注入合成 key）→ 执行 A–F 与别名断言 → 测试结束清理子进程。

逐项结果与证据见 [CONFORMANCE.md](CONFORMANCE.md)；证据 JSON 与代理日志输出到 `tests/.run/`。

## 目录结构

```text
litellm/
├── pyproject.toml       # 依赖与精确版本锁定
├── uv.lock              # uv 锁定文件
├── config.yaml          # 唯一配置事实源
├── CONFORMANCE.md       # conformance 报告（含发现的偏差）
├── README.md            # 本文档
└── tests/
    ├── fake_provider.py # OpenAI 兼容 fake provider（记录上游请求）
    ├── conftest.py      # 子进程启动/清理代理与 fake provider
    ├── evidence.py      # conformance 证据收集
    └── test_conformance.py
```

### Codex 模型路由桥接（config.agent.yaml + scripts/start-agent-gateway.ps1）

- 作用：Codex 统一经本项目 LiteLLM 路由到 ChatGPT 账号。
- 拓扑：Codex → zstd 解压代理(127.0.0.1:4100, `tools/agent-zstd-proxy.py`) →
  LiteLLM(127.0.0.1:4101, `config.agent.yaml`) → 原生 `/responses` 上游。
- 账号路由：GPT family 经 `chatgpt/` provider 走 OpenAI 账号（复用
  `~/.codex/auth.json` 登录态，无需 API key），默认 base
  `https://chatgpt.com/backend-api/codex`，消耗账号余额。
- 模型列表由 `scripts/sync-agent-gpt-models.ps1` 在网关启动前生成：读取
  `%USERPROFILE%\.codex\models_cache.json` 中 `visibility=list` 的官方模型，
  并补充官方 Codex 模型页已公布但本地缓存尚未刷新时的
  `gpt-6-astra`；这些条目以官方名称进入 LiteLLM `/v1/models`，并通过
  `chatgpt/<official-model>` provider route 转发。Codex 自身继续使用 Codex 管理的
  官方运行时目录，本项目不写入或替换 Codex 模型目录；网关重启后即可加载新的
  LiteLLM 列表。
- 运行中的网关不会因模型目录变化而后台重启；这样避免中断活动 Codex 请求。
  若要立即应用新列表，请在当前请求结束后运行
  `scripts/start-agent-gateway.ps1`。
- 为什么需要代理：codex 请求体带 `Content-Encoding: zstd`，LiteLLM/FastAPI
  不解压导致 model=None（400）；aiohttp 3.14 会自动解压 zstd，代理据此仅对
  仍以 zstd magic 开头的 body 手动解压。
- 上下文修复：启动脚本用最小 `CHATGPT_DEFAULT_INSTRUCTIONS` 覆盖 LiteLLM
  1.96.0 每请求重复注入的 7.5 KB 旧 Codex 提示，不降低模型的 1M 窗口。
- 子智能体修复：代理临时别名化 `collaboration` 工具以关闭跨 provider 不可解密
  的消息参数，再把 `agent_message` 转成 OpenCode Go 能读取的标准 user message。
- 旧 OpenCode Go 工具兼容代码仍保留在代理中，但当前 GPT-only 配置不会启用该路由；
  因而 OpenCode Go 的 schema 变换不会影响当前 `/v1/models`。
- 超长会话截断：超过 950k token 预算时代理截断最旧条目，并按 `call_id` 对账
  工具调用/输出配对，删除被截断调用遗留的孤儿 `function_call_output`，避免
  OpenCode Go 以 "No tool call found for tool output" 整体拒绝请求。
- 网页检索修复：ChatGPT provider 即使收到 `stream:false` 仍返回 SSE；代理聚合
  `response.output_item.done`/`response.completed`，返回标准 Responses JSON。
- 日志只记录请求大小、item 类型/计数、工具数量和转换计数，不记录正文或参数。
- zstandard 依赖已加入 pyproject/uv.lock；`uv sync --locked` 可复现。
- 当前 GPT-only 路由复用 `~/.codex/auth.json` 登录态，不需要 provider API key；
  旧 OpenCode Go / DeepSeek 路由若恢复，仍只能经受限环境变量注入，不能写入任何
  配置、日志或仓库。
- `scripts/start-agent-gateway.ps1` 会替换 4100/4101 监听；不要在仍有活动 Codex
  请求时执行。
