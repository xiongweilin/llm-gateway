# LiteLLM Gateway 重建配置

本文记录当前本机模型网关的非敏感配置。

## 目录和拓扑

- 仓库：xiongweilin/litellm-gateway
- 目标目录：D:\agent\litellm-gateway
- LiteLLM 项目目录：D:\agent\litellm-gateway\litellm
- 4100：agent-zstd-proxy.py，监听 127.0.0.1:4100，提供 Codex Responses/zstd 兼容层。
- 4101：LiteLLM，监听 127.0.0.1:4101，提供 OpenAI 兼容网关。
- Codex 路径：Codex -> 4100 -> 4101 -> ChatGPT GPT family 或 OpenCode Go。
- 当前模型集合：gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna、
  opencode-go/muse-spark-1.3-contributor。
- OpenCode Go 路由的 LiteLLM `timeout` 与 `stream_timeout` 为 3600 秒；4100 代理的
  上游总超时为 4200 秒，用于覆盖长时间推理中的流式空闲间隔。
- Codex 工具兼容：`custom_tool_call` 在发往 OpenCode Go 时临时转为单字符串
  function，响应返回 Codex 前再还原；namespace 函数调用会恢复为声明的 namespace/name。
- 默认模型：gpt-5.6-luna。
- 运行时不使用数据库；配置事实源是 config.agent.yaml，测试配置是 config.yaml。

## 依赖和入口

- Python 依赖由 litellm/pyproject.toml 与 litellm/uv.lock 锁定。
- 安装：cd D:\agent\litellm-gateway\litellm；uv sync --locked。
- 当前正式路由启动入口：scripts/start-agent-gateway.ps1。
- 模型目录同步入口：scripts/sync-agent-gpt-models.ps1。
- 健康检查：4100/health/liveliness 与 4101/health/liveliness。
- 启动前应确保当前用户环境中存在 `OPENCODEGO_API_KEY`，不把值写入配置、日志或仓库。

## 验证

uv sync --locked
uv run pytest -v
Invoke-RestMethod http://127.0.0.1:4100/health/liveliness
Invoke-RestMethod http://127.0.0.1:4101/health/liveliness
