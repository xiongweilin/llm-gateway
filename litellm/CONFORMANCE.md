# LiteLLM 网关 conformance 报告

> 本报告记录 `gateway/litellm` 独立 LiteLLM 模型网关的合规性验证结果。
> 所有证据均来自真实测试执行（`uv run pytest`），非纸面假设。

## 1. 组件声明

本组件是个人 Agent 项目（`D:\agent\litellm-gateway`）内的独立网关组件。

- LiteLLM 使用**项目内独立 uv 环境**（`gateway/litellm/.venv` 与 `uv.lock`），不写入系统级 Python。
- 网关**无数据库运行**：不设置 `database_url`、`store_model_in_db: false`，配置只来自 `config.yaml`。
- 本目录外的任何文件一律未被修改（仓库根 `.gitignore` 存在与本任务无关的既有改动，未触碰）。

## 2. 环境与锁定版本

| 项 | 值 |
| --- | --- |
| 操作系统 / Shell | Windows / PowerShell（pwsh） |
| uv | 0.12.3 |
| Python（项目解释器） | CPython 3.14.6（`uv sync` 自动选用，无需降级 3.12） |
| LiteLLM | **1.96.0**（`litellm[proxy]==1.96.0` 精确锁定，见 [pyproject.toml](pyproject.toml)） |
| fastapi | 0.136.3（固定 0.136.x，见偏差 ①） |
| prisma | 0.15.0（精确锁定，见偏差 ②） |
| pytest | 9.1.1（dev 依赖，精确锁定） |
| openai / httpx | 2.53.0 / 0.28.1（随 litellm 解析） |
| 锁定文件 | `uv.lock`（`uv sync --locked` 通过） |

验证命令（均已真实执行并成功）：

```powershell
uv sync --locked
uv run pytest
```

## 3. 配置事实源

`config.yaml` 是唯一配置事实源，要点：

- `model_list`：conformance fake provider（`model: fake-responses`，`custom_llm_provider: openai`，`api_base`/`api_key` 均以 `os.environ/` 引用）；deepseek/openai 真实 provider 以注释模板存在（key 全部 `os.environ/` 引用，无明文）。
- `router_settings.model_group_alias`：稳定别名 `fake-gw -> fake-responses`。
- `litellm_settings`：`cache: false`（关闭 response cache）、不启用 semantic cache（无 `cache_params`/`redis-semantic`）、`callbacks/success_callback/failure_callback: []`（关闭外部日志 callbacks）、`turn_off_message_logging: true`、`suppress_debug_info: true`、`telemetry: false`；**不启用 detailed_debug**（CLI 不传 `--detailed_debug`，也不开 `set_verbose`）。
- `general_settings`：`master_key: os.environ/LITELLM_MASTER_KEY`（环境变量注入，无明文）、`store_model_in_db: false`（无 DB）、`cancel_on_disconnect: true`。
- 绑定：仅 loopback（启动命令 `--host 127.0.0.1`，见偏差 ③）。

## 4. 逐项 conformance 结果

测试共 **9 项，全部通过**（`9 passed in 30.64s`，pytest 退出码 0；`uv sync --locked` 退出码 0）。证据 JSON 见 `tests/.run/evidence.json`，代理与测试日志见 `tests/.run/`。

### A. 消息 / 工具顺序保持 —— PASS

经 `/v1/responses` 发送 3 条消息（system + 2 user）+ 2 个工具定义，上游 fake provider 实际收到：

- 消息 canary 顺序：`SYS_CANARY_0 → USER_CANARY_1 → USER_CANARY_2`（逐条出现一次，无重排、无额外注入）。
- role 顺序：`[system, user, user]`，消息条数 = 3（未注入多余消息）。
- 工具顺序：`canary_tool_alpha → canary_tool_beta`，描述文本顺序一致。
- 消息条目键集合仅为 `{content, role}`：**消息内部无时间戳、无 request_id、无 id 注入**。
- 上游请求路径：`POST /v1/responses`（未转换为 chat/completions）。
- 客户端响应 `output` 类型序列 `[reasoning, message]`，正文回显正确。

### B. 稳定前缀无动态字节 —— PASS

完全相同请求发送两次，断言：

- 上游消息数组 JSON 字节一致：`input_bytes_identical = true`。
- 上游 `tools` 字节一致：`tools_bytes_identical = true`。
- 去除顶层动态键（`id`/`created_at` 等）后的整包 body 一致：`normalized_full_body_identical = true`。
- 上游请求顶层键仅 `{input, model, tools}` —— 无 litellm 注入的动态字段。

### C. cache 参数转发与 usage 标准化（/v1/responses）—— PASS

请求携带 `prompt_cache_key`、`prompt_cache_retention`、输入条目级 `caching: {ephemeral: true}`，以及非标准的 `prompt_cache_options`：

| 参数 | 上游实际收到 | 结论 |
| --- | --- | --- |
| `prompt_cache_key: canary-cache-key-1` | 原样转发 | 通过 |
| `prompt_cache_retention: 5m` | 原样转发 | 通过 |
| 输入条目 `caching: {ephemeral: true}` | 原样转发 | 通过 |
| `prompt_cache_options`（非标准） | 被丢弃（未到达上游） | 如实记录：litellm 仅识别白名单参数 |

响应 `usage` 实际映射：`usage.input_tokens_details.cached_tokens = 1234`，provider 返回的缓存 usage **原样透传**。

### D. streaming / tool_calls / reasoning / 取消 —— PASS

- SSE 事件顺序完整（17 个事件）：`response.created → in_progress → output_item.added(reasoning) → reasoning_summary_text.delta/done → output_item.done → output_item.added(function_call) → function_call_arguments.delta/done → output_item.done → output_item.added(message) → content_part.added → output_text.delta/done → content_part.done → output_item.done → response.completed`。
- reasoning 文本与 function_call 名称/参数在流中可见；正文 delta 完整。
- 注：代理下发的流不携带 `event:` 行，事件类型位于 `data` JSON 的 `type` 字段内（OpenAI SDK 兼容格式），以 `data: [DONE]` 收尾。
- **取消传播**：客户端读完 2 个事件后主动断开，上游 fake provider 在 20s 内记录到连接中断（`upstream_abort_events` 非空）→ 取消语义已传播到上游。

### E. 认证与绑定 —— PASS（错 key 状态码偏差见 5-④）

- 缺失 key → **401**。
- 错 key（`sk-wrong-canary-9999`）→ 400 `no_db_connection`（被拒绝，但非 401，见偏差 ④）。
- 正确 key → 200。
- `/v1/models` 带错 key → 被拒绝（400）。
- **仅绑定 loopback**：`netstat` 显示监听地址仅为 `127.0.0.1:<port>`；对本机 5 个非 loopback IPv4（`100.101.64.52`、`172.18.0.1`、`172.20.0.1`、`172.31.112.1`、`192.168.0.100`）访问全部不可达（连接拒绝/超时）。

### F. usage 标准化（/v1/chat/completions）—— PASS

chat completions 响应 usage 实际映射：`usage.prompt_tokens_details.cached_tokens = 1234`，原样透传；消息顺序同样保持（`CHAT_SYS_CANARY → CHAT_USER_CANARY`）。

### 稳定别名 —— PASS

客户端以别名 `fake-gw` 请求 `/v1/responses` → 200，上游实际收到模型 `fake-responses`，别名解析成功。

## 5. 发现的偏差（如实记录，未粉饰）

1. **fastapi 版本钉死 0.136.x（上游元数据 bug）**：litellm 1.96.0 声明 `fastapi>=0.136.3,<1.0`，但其代理代码仍 `import fastapi.dependencies.utils.get_flat_dependant`；该符号在 fastapi ≥0.137 被移除。uv 默认解析到 fastapi 0.141.1 导致代理启动即崩（`ImportError`）。处理：在 [pyproject.toml](pyproject.toml) 显式固定 `fastapi>=0.136.0,<0.137.0`（锁到 0.136.3）。
2. **prisma 依赖（DB-less 认证 401 修复）**：litellm 1.96.0 认证异常处理器无条件 `import prisma`（`proxy/db/exception_handler.py`），未安装时任何认证失败（缺失/错误 key）都会从 401 变成 500。安装 `prisma==0.15.0` 后缺失 key 恢复 401；**prisma 仅被 import，未配置 `database_url`，不建立任何数据库连接**（无数据库运行保持不变）。
3. **loopback 绑定来自 CLI 而非 config**：litellm 1.96.0 CLI 的 `--host` 默认为 `0.0.0.0`，config.yaml 无 host 键。本仓库把“仅绑定 loopback”作为标准运行方式（`--host 127.0.0.1`，见 [README.md](README.md)），conformance 以实际监听地址与可达性负测试验证。
4. **错 key 返回 400 而非 401**：DB-less 模式下，任何非 master key（含错误 key）在 `user_api_key_auth` 中命中 `prisma_client is None → ProxyException("No connected db.", code=400)`（litellm 1.96.0 固有设计）。请求被拒绝，但状态码为 400 而非规范期望的 401，如实记录为部分达标。
5. **流式重组丢弃 message item**：`/v1/responses` 流式请求的最终 `response.completed` 事件中，litellm 重组后的 `output` 仅含 `[reasoning, function_call]`，**assistant message item 未包含在内**（正文已通过 `output_text.delta/done` 完整下发，客户端实测可见）。属 litellm 1.96.0 流式重组行为，如实记录。
6. **Admin UI 静态页面仍挂载**：litellm 1.96.0 无“关闭 Admin UI”配置键，`/ui` 静态面板仍挂载；但本部署无数据库，`/key`、`/user`、`/team` 等管理接口不可用（`no_db_connection`），管理功能实质关闭。
7. **SSE 无 `event:` 行**：代理把上游事件转成 `data: {"type": ...}` 格式下发（OpenAI SDK 兼容），并在每个事件 data 中附加 `model` 字段；事件类型仍可追踪，顺序与内容未受影响。

## 6. 复现步骤

```powershell
cd D:\agent\litellm-gateway\gateway\litellm
uv sync --locked
# 测试会自动：启动 fake provider → 以临时端口启动 litellm 代理子进程（注入
# LITELLM_MASTER_KEY / FAKE_PROVIDER_API_KEY / FAKE_PROVIDER_API_BASE）→ 运行断言 → 清理子进程
uv run pytest -v
```

证据产物：`tests/.run/evidence.json`（逐项证据）、`tests/.run/litellm-proxy.log`（代理日志，仅含合成 canary 内容）。

## 追加记录（2026-08-11，P3 集成发现）

- **litellm 内部重试必须关闭**：litellm Router 默认 `num_retries=2`，会对 provider
  5xx **静默重试**并返回成功——这会绕过 daemon 的 Run 生命周期（计划要求 provider
  重试以 `retry_of` 新建 Run 且可审计）。`config.yaml` 与 e2e 的
  `litellm-config.yaml` 均在 `router_settings.num_retries: 0` 关闭；e2e 故障注入
  场景 d（模型 500 → `run.failed` + `run.retried`）验证：关闭后错误真实到达
  daemon，kernel 级重试事件正确落账。
- 该发现同步为生产配置事实：重试所有权在 daemon（`max_run_attempts`），
  LiteLLM 只做一次转发。
