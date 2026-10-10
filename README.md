# LLM Gateway

[![CI](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Docs: EN / 中文](https://img.shields.io/badge/docs-EN%20%7C%20%E4%B8%AD%E6%96%87-blue.svg)](README.zh-CN.md)

[English](README.md) | [简体中文](README.zh-CN.md)

A self-owned model-routing core with a stable Agent entry and dedicated
Responses, Chat Completions, and Anthropic Messages protocol services. Provider
routing and protocol compatibility are implemented in this repository.

## Service topology

```text
Codex, Agent, and Anthropic clients ──► 4101 Unified Agent entry
                                           ├── `/v1/responses` ───────► 4102 Responses service ─┐
                                           ├── `/v1/chat/completions` ► 4103 Chat service ───────┤
                                           └── `/v1/messages` ────────► 4104 Messages service ───┴──► 4100 Core ──► provider API

For `sonnet-5.5`, 4101 converts Codex Responses requests into Anthropic Messages requests and forwards them to 4104.
```

- **4100 — Core:** owns route selection, outbound provider requests,
  rate-limit retries, model catalog, health, and provider authentication.
  Codex/ChatGPT subscription routes use the gateway-owned ChatGPT OAuth store;
  other providers use their configured credential source.
- **4101 — Unified Agent entry:** the single local endpoint for Codex, Agent,
  and Anthropic clients; dispatches all three protocols and converts Codex
  Responses requests for models marked `codex_responses`.
- **4102 — Responses protocol:** owns Responses-only compatibility, streaming,
  and compaction before forwarding to Core. It immediately forwards streamed
  SSE events and does not translate Responses to Chat Completions.
- **4103 — Chat Completions protocol:** preserves Chat request/response and SSE
  streaming semantics while forwarding to Core.
- **4104 — Anthropic Messages protocol:** forwards `/v1/messages` and
  `/v1/messages/count_tokens` while preserving JSON and SSE streaming semantics.

All listeners bind to the host and ports declared in `config/gateway.json`.
The default host is loopback. The port roles are defined there once and are
consumed by the runtime scripts.

## Model routes

`core/models.yaml` is the route source of truth. Each entry declares the public
model id, protocol mode, provider model id, API base, and credential environment
variable name. `codex_responses: true` enables the 4101 Responses-to-Messages
adapter for a Messages model. Anthropic Messages routes inject the provider key as `x-api-key`
and default the API version to `2023-06-01`. The `sonnet-5.5` provider key is
read from the `KITOOL_API_KEY` environment variable; its provider-side model
id is `claude-sonnet-5-5`.
Credential values are never stored in the route file. `api_base_env` allows
provider endpoints that explicitly opt in to be overridden without editing
code. Codex GPT routes pin `https://chatgpt.com/backend-api/codex` and use
`authorization: chatgpt`. This matches the historical LiteLLM `chatgpt/*`
provider contract: Codex keeps its built-in OpenAI provider and points
`openai_base_url` at the local Agent entry, while Core replaces the client's
API-key-shaped Authorization value with a ChatGPT subscription OAuth token.
The gateway token store is `~/.config/llm/chatgpt/auth.json`; refresh and
device-code login use the same OAuth flow as the previous LiteLLM path.

The Core sends requests directly to the configured provider API. The public
model id remains stable while `upstream_model` selects the provider-side model.

## Local development

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

Stop the five local services with:

```powershell
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

The start/stop scripts resolve the repository root relative to themselves and
read host, ports, and route locations from `config/gateway.json`. They do not
require a fixed checkout path.
