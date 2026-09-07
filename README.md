# LiteLLM Gateway

[![CI](https://github.com/xiongweilin/litellm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/xiongweilin/litellm-gateway/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

LiteLLM Gateway provides protocol-specific OpenAI-compatible ingress and
centralized model routing for local runtimes.

## Responsibility boundary

This repository owns:

- protocol ingress
- model routing
- gateway deployment
- transport and upstream compatibility required for routing

This repository does not own:

- runtime lifecycle
- run or work ownership
- action authorization
- effect execution
- task completion semantics

## Fixed topology

```text
Responses or Chat client -> 127.0.0.1:4100 -> 127.0.0.1:4101 -> upstream
                                     │
                                     └─ Chat mode via 127.0.0.1:4102
                                        -> 127.0.0.1:4101 -> upstream
```

Port 4100 is the unified public protocol ingress. It accepts both
`/v1/responses` and `/v1/chat/completions`, and exposes the union of the
runtime model catalog. A Responses request for a chat-mode model is converted
and forwarded through the Chat Completions hop on port 4102. Port 4102 remains
available as a direct Chat Completions entry point and exposes only chat-mode
models. The model catalog is derived from the protocol mode in
`litellm/models.yaml`.

## Repository layout

- `litellm/models.yaml`: gateway-owned model and route source.
- `litellm/config.runtime.template.yaml`: non-generated runtime settings.
- `litellm/config.runtime.yaml`: generated runtime configuration.
- `scripts/generate-runtime-config.ps1`: deterministic runtime config generator.
- `tools/responses-proxy.py`: unified Responses/Chat protocol ingress and
  Responses-to-Chat bridge.
- `tools/chat-completions-proxy.py`: Chat Completions forwarding hop and direct
  Chat entry point.
- `tools/protocol_models.py`: runtime protocol catalog loader.

The existing lifecycle script filenames are retained for scheduled-task
compatibility. Their public behavior is protocol-neutral.

## Local development

```powershell
Set-Location D:\agent\litellm-gateway\litellm
uv sync --locked
Set-Location D:\agent\litellm-gateway
pwsh -NoProfile -File .\scripts\generate-runtime-config.ps1
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

Run `scripts\stop-agent-gateway.ps1` when the local services should be
stopped. Start and stop operations replace processes listening on ports
4100–4102, so perform them at a safe operational boundary.

## Security

The local listeners bind to loopback. Credentials referenced by the runtime
model configuration must be supplied through environment variables; secret
values must not be committed to configuration, logs, or source control.
