# LiteLLM Gateway 重建配置

本文记录当前本机模型网关的非敏感配置。

## 目录和拓扑

- 仓库：xiongweilin/litellm-gateway
- 目标目录：D:\agent\litellm-gateway
- LiteLLM 项目目录：D:\agent\litellm-gateway\litellm
- 4100：Responses 入口，监听 127.0.0.1:4100；保留 zstd 解压和必要的
  Responses 兼容层。
- 4101：LiteLLM，监听 127.0.0.1:4101，提供统一模型路由。
- 4102：Chat Completions 入口，监听 127.0.0.1:4102；透明转发 SSE 到 4101。
- 协议路径：Responses -> 4100 -> 4101；Chat Completions -> 4102 -> 4101。
- 上游模型分开使用协议路由：Muse 使用 Responses；Omen 保留 Responses 兼容别名
  `opencode-go/omen-alpha`，并通过 `omen-alpha` 走 4102 的原生 Chat Completions
  路由。Omen deployment 单独启用 `drop_params: true`，丢弃不支持的
  `reasoning_effort`，不启用全局丢参。
- `/v1/alpha/*` 是 4100 上的 control-plane 旁路，映射到
  `https://chatgpt.com/backend-api/codex` 的 `/alpha/*`，不送入 LiteLLM。
- 当前模型集合：gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna、
  `opencode-go/muse-spark-1.3-contributor`、`muse-spark-1.3-contributor`、
  `opencode-go/omen-alpha`，以及 Chat Completions 原生别名 `omen-alpha`。
- LiteLLM 与上游部署的 `timeout`/`stream_timeout` 为 3600 秒；4100/4102
  入口的上游总超时为 4200 秒，用于覆盖长时间推理中的流式空闲间隔。
- Responses 工具兼容：`custom_tool_call` 在发往上游时临时转为单字符串
  function，响应返回 Codex 前再还原；namespace 函数调用会恢复为声明的 namespace/name。
- 默认模型：gpt-5.6-luna。
- 运行时不使用数据库；配置事实源是 config.agent.yaml，测试配置是 config.yaml。

## 依赖和入口

- Python 依赖由 litellm/pyproject.toml 与 litellm/uv.lock 锁定。
- 安装：cd D:\agent\litellm-gateway\litellm；uv sync --locked。
- 当前正式路由启动入口：scripts/start-agent-gateway.ps1。
- 模型目录同步入口：scripts/sync-agent-gpt-models.ps1。
- 健康检查：4100/4101/4102 的 `/health/liveliness`。
- 启动前应确保当前用户环境中存在 `OPENCODEGO_API_KEY`，不把值写入配置、日志或仓库。

## 客户端入口配置

- Responses 客户端使用 `http://127.0.0.1:4100/v1`，SDK 类型为
  `@ai-sdk/openai`；GPT 与 `muse-spark-1.3-contributor` 使用此入口。
- Chat Completions 客户端使用 `http://127.0.0.1:4102/v1`，SDK 类型为
  `@ai-sdk/openai-compatible`；`omen-alpha` 使用此入口。
- 本机客户端配置里的 `apiKey` 只需使用本地占位值（例如 `local-opencode`）；
  上游密钥仍只由 4101 所在进程读取 `OPENCODEGO_API_KEY`，不要复制到客户端配置。

## 验证

uv sync --locked
uv run pytest -v
Invoke-RestMethod http://127.0.0.1:4100/health/liveliness
Invoke-RestMethod http://127.0.0.1:4101/health/liveliness
Invoke-RestMethod http://127.0.0.1:4102/health/liveliness
