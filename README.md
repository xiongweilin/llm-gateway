# LLM Gateway

[![CI](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/xiongweilin/llm-gateway/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A self-owned model-routing core with one stable Agent entry and a dedicated
Responses protocol service. Provider routing and protocol compatibility are
implemented in this repository.

## Service topology

```text
Agent clients
     │
     ▼
4101  Unified Agent entry
     ├── Chat Completions ──────────────► 4100  Core
     └── Responses protocol ────────────► 4102  Responses service
                                               │
                                               └────────► 4100  Core ──► provider API
```

- **4100 — Core:** owns route selection, provider credentials, outbound model
  requests, rate-limit retries, model catalog, and health.
- **4101 — Unified Agent entry:** the single local endpoint for agent clients;
  dispatches requests by protocol and exposes the combined model catalog.
- **4102 — Responses protocol:** owns Responses-specific compatibility,
  streaming, compaction, and translation for chat-only routes.

All listeners bind to the host and ports declared in `config/gateway.json`.
The default host is loopback. The port roles are defined there once and are
consumed by the runtime scripts.

## Model routes

`core/models.yaml` is the route source of truth. Each entry declares the public
model id, protocol mode, provider model id, API base, and credential variable
name. Credential values are read from the process environment and are never
stored in the route file. `api_base_env` allows the provider endpoint to be
overridden without editing code.

The Core sends requests directly to the configured provider API. The public
model id remains stable while `upstream_model` selects the provider-side model.

## Local development

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

Stop the three local services with:

```powershell
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

The start/stop scripts resolve the repository root relative to themselves and
read host, ports, and route locations from `config/gateway.json`. They do not
require a fixed checkout path.
