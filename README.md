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

## Muse context handling

The Muse Spark 1.3 Contributor route uses a gateway-side precompaction guard on
4100 only. When the estimated Muse request reaches the configured precompaction
budget, 4100 generates a bounded checkpoint, keeps the newest tool-call/result
history, rotates to a fresh upstream `x-opencode-session` epoch, and forwards a
normal Responses request to 4101. The Codex thread remains unchanged; the
provider session is the state boundary that is replaced after the first
successful compaction. During a streamed checkpoint operation it sends SSE
comments as connection heartbeats; it does not emit a visible waiting message,
a fake tool call, or a remote compaction output item.

If Codex still sends a `compaction_trigger` from an existing or stale session,
4100 removes that marker before the Muse precompaction decision and never
forwards it to LiteLLM or the provider. Repeated requests reuse the rotated
provider epoch and the existing checkpoint state instead of opening another
provider session for the same compacted prefix.

The default Muse precompaction budget is `900000` estimated tokens, below the
current Codex effective threshold of about `950000`. It can be tuned with
`MUSE_PRECOMPACTION_TOKEN_BUDGET`. The compaction state is in-memory and keyed
by the original opaque Codex session plus the conversation head; a gateway
restart discards it and the next long request rebuilds both the checkpoint and
the provider session epoch. GPT routes, Omen, and the 4102 Chat Completions path
do not use this guard.

The Codex model catalog marks Muse with `use_responses_lite=false` and an
`auto_compact_token_limit` of `900000`. The gateway still handles any
`compaction_trigger` emitted by an existing Codex session locally. GPT and Omen
retain their existing capability flags. Catalog changes do not restart Codex;
restart it separately after the user chooses a safe boundary.

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
