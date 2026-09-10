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

## Request retry and timeout boundary

The gateway does not use the previous per-model `timeout` or
`stream_timeout` values. The upstream model request may therefore use
LiteLLM's own fallback behavior; the gateway does not define a 900-second or
3600-second model deadline in the route source.

The runtime router keeps general retries disabled and enables four retries only
for `RateLimitError`. LiteLLM uses the upstream `Retry-After` response header
when it is a usable short delay, and falls back to its bounded exponential
backoff otherwise. 400/401/403, quota/configuration failures, timeouts, and
other error classes are not enabled by this policy.

## Muse and DeepSeek Flash context handling

Codex owns when the Muse Spark 1.3 Contributor or DeepSeek Flash conversation is
compacted. The gateway does not proactively summarize normal requests or rotate
the provider session just because a local estimate reaches a threshold.

When Codex sends a `compaction_trigger`, 4100 handles that protocol boundary
locally: it creates a model-specific OpenCode Go checkpoint and returns a Responses
`response.compaction` containing the retained user messages followed by exactly
one gateway compaction item. On the next request for either model, 4100 resolves
the opaque checkpoint handle back into a normal historical message before
forwarding the request, so OpenCode Go receives only input item types it
supports. This path runs only for an explicit Codex compaction request.

The compaction target budget is `900000` estimated tokens and can be tuned with
`MUSE_COMPACTION_TOKEN_BUDGET`. GPT routes, Omen, and the 4102 Chat Completions
path do not use this Responses compaction checkpoint path. The Codex model
catalog marks Muse and DeepSeek Flash with `use_responses_lite=false` and an
`auto_compact_token_limit` of `900000`.

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
