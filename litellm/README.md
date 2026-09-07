# litellm-gateway —— 项目内独立 LiteLLM 模型网关

> 为个人 Agent 项目提供独立的 LiteLLM 模型网关组件；运行配置使用
> `config.agent.yaml`。

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

Conformance 环境的配置事实源是 [config.yaml](config.yaml)；运行路由的配置事实源是
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

### 协议入口与模型路由（config.agent.yaml + scripts/start-agent-gateway.ps1）

- 拓扑固定为：Responses `127.0.0.1:4100`、Chat Completions
  `127.0.0.1:4102`，二者都转发到 LiteLLM `127.0.0.1:4101`。
- 4100 负责 Responses 兼容和 zstd 请求体解压；`/v1/alpha/*` 是现有
  control-plane 旁路，不作为模型 API 送入 LiteLLM。
- 4102 只负责 Chat Completions 转发，保留 SSE，不把 Chat 请求转换成
  Responses。上游需要会话头的模型由该入口补入 `extra_headers`，其他模型的
  请求体保持不变。
- 模型路由分开维护：GPT 使用 `chatgpt/<model>`；Muse 使用上游 Responses
  路由；Omen 同时保留 Responses 兼容别名 `opencode-go/omen-alpha`，并新增
  Chat Completions 原生别名 `omen-alpha`。Omen deployment 仅本地启用
  `drop_params: true`，丢弃不支持的 `reasoning_effort`，不启用全局丢参。
- 模型目录由 `scripts/sync-agent-gpt-models.ps1` 生成区块维护；非生成路由由
  `config.agent.template.yaml` 持有。这样目录刷新不会删除协议路由。
- Responses 入口保留必要的工具兼容、会话头、超长输入截断和非流式 SSE 聚合；
  这些转换只在对应请求形状/模型路由上触发，不改变 GPT 的正常路由。
- 两个协议入口的上游请求超时为 4200 秒，LiteLLM 与上游部署的超时为 3600 秒，
  覆盖长时间推理的流式空闲间隔。
- GPT 路由复用 `~/.codex/auth.json` 登录态，不需要 provider API key；上游
  provider key 只通过当前用户环境变量 `OPENCODEGO_API_KEY` 注入，不能写入
  配置、日志或仓库。
- 启动脚本会替换 4100/4101/4102 监听；不要在仍有活动请求时执行。模型列表和
  配置变更需要由用户在安全时机运行 `scripts/start-agent-gateway.ps1`，并重新
  启动需要重新读取配置的客户端。
