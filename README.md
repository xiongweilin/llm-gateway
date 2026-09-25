# LLM Gateway

[![CI](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A self-owned model-routing core with one stable Agent entry and dedicated
Responses and Chat Completions protocol services. Provider routing and protocol
compatibility are implemented in this repository.

## Service topology

```text
Agent clients
     │
     ▼
4101  Unified Agent entry
     ├── Responses ─────────────────────► 4102  Responses service ─┐
     └── Chat Completions ──────────────► 4103  Chat service ──────┴──► 4100 Core ──► provider API
```

- **4100 — Core:** owns route selection, outbound provider requests,
  rate-limit retries, model catalog, and health. Provider credentials are
  either supplied by the configured provider secret or, for Codex/ChatGPT
  subscription routes, forwarded from the authenticated Agent client.
- **4101 — Unified Agent entry:** the single local endpoint for agent clients;
  dispatches Responses and Chat requests to their protocol services and exposes
  the combined model catalog.
- **4102 — Responses protocol:** owns Responses-only compatibility, streaming,
  and compaction before forwarding to Core. It immediately forwards streamed
  SSE events and does not translate Responses to Chat Completions.
- **4103 — Chat Completions protocol:** preserves Chat request/response and SSE
  streaming semantics while forwarding to Core.

All listeners bind to the host and ports declared in `config/gateway.json`.
The default host is loopback. The port roles are defined there once and are
consumed by the runtime scripts.

## Model routes

`core/models.yaml` is the route source of truth. Each entry declares the public
model id, protocol mode, provider model id, API base, and authorization source.
Credential values are never stored in the route file. `api_base_env` allows
provider endpoints that explicitly opt in to be overridden without editing
code. Codex GPT routes pin `https://chatgpt.com/backend-api/codex` and use
`authorization: client`: the Codex client is configured as an explicit
`llm-gateway` model provider with `requires_openai_auth = true`, so Codex
itself resolves the active ChatGPT Plus OAuth credential from its supported
credential store and the gateway preserves that authenticated request. The
gateway does not read or duplicate Codex's private credential storage.

The Core sends requests directly to the configured provider API. The public
model id remains stable while `upstream_model` selects the provider-side model.

## Local development

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

Stop the four local services with:

```powershell
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

The start/stop scripts resolve the repository root relative to themselves and
read host, ports, and route locations from `config/gateway.json`. They do not
require a fixed checkout path.
