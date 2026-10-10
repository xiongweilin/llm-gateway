# LLM Gateway

[![CI](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Docs: EN / 中文](https://img.shields.io/badge/docs-EN%20%7C%20%E4%B8%AD%E6%96%87-blue.svg)](README.zh-CN.md)

[English](README.md) | [简体中文](README.zh-CN.md)

一个自主管理的模型路由核心，提供稳定的 Agent 入口，以及独立的 Responses、Chat Completions 和 Anthropic Messages 协议服务。Provider 路由和协议兼容逻辑都由本仓库实现。

## 服务拓扑

```text
Agent 客户端 ──► 4101 统一 Agent 入口
                     ├── Responses ───► 4102 Responses 服务 ─┐
                     └── Chat ─────────► 4103 Chat 服务 ─────┤
Anthropic 客户端 ───────────────────────► 4104 Messages 服务 ─┴──► 4100 Core ──► provider API
```

- **4100 — Core：**负责路由选择、向 provider 发出请求、限流重试、模型目录、健康状态以及 provider 身份验证。Codex/ChatGPT 订阅路由使用 gateway 自己维护的 ChatGPT OAuth 存储；其他 provider 使用各自配置的认证来源。
- **4101 — 统一 Agent 入口：**Agent 客户端唯一的本地端点；把 Responses 和 Chat 请求分发到对应协议服务，并暴露合并后的模型目录。
- **4102 — Responses 协议：**负责 Responses 专属兼容、流式传输以及转发到 Core 之前的 compaction。它会立即转发流式 SSE 事件，不会把 Responses 转换成 Chat Completions。
- **4103 — Chat Completions 协议：**在转发到 Core 时保持 Chat 请求/响应以及 SSE 流式语义。
- **4104 — Anthropic Messages 协议：**原样转发 `/v1/messages` 和 `/v1/messages/count_tokens`，保持 JSON 与 SSE 流式语义。

所有 listener 都绑定到 `config/gateway.json` 中声明的 host 和 port。默认 host 是 loopback。各端口角色只在该文件中定义一次，由运行脚本共同读取。

## 模型路由

`core/models.yaml` 是路由的事实源。每个条目声明公开 model id、协议模式、provider model id、API base 和凭据环境变量名称。`sonnet-5.5` 映射到 Kitool 的 `claude-sonnet-5-5`，provider key 从 `KITOOL_API_KEY` 环境变量读取；Anthropic Messages 路由会将其注入 `x-api-key`，缺省版本为 `2023-06-01`。

认证值不会保存在路由文件中。`api_base_env` 允许明确选择启用覆盖的 provider endpoint 在不修改代码的情况下被替换。Codex GPT 路由固定使用 `https://chatgpt.com/backend-api/codex`，并设置 `authorization: chatgpt`。这与历史 LiteLLM `chatgpt/*` provider 契约一致：Codex 保留内置 OpenAI provider，把 `openai_base_url` 指向本地 Agent 入口，而 Core 会把客户端形似 API key 的 Authorization 值替换为 ChatGPT 订阅 OAuth token。

Gateway token store 位于 `~/.config/llm/chatgpt/auth.json`；刷新和 device-code 登录使用与此前 LiteLLM 路径相同的 OAuth 流程。

Core 会直接向配置的 provider API 发送请求。公开 model id 保持稳定，而 `upstream_model` 选择 provider 侧的模型。

## 本地开发

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

停止五个本地服务：

```powershell
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

启动/停止脚本会相对于自身位置解析仓库根目录，并从 `config/gateway.json` 读取 host、port 和路由位置，因此不依赖固定 checkout 路径。
