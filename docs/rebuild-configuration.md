# Runtime configuration rebuild

This document describes how to recreate the local gateway deployment without
including secret values or a model inventory.

## Repository location

```text
D:\agent\llm-gateway
```

The LiteLLM Python environment is under:

```text
D:\agent\llm-gateway\litellm\.venv
```

Install or refresh the locked environment with:

```powershell
Set-Location D:\agent\llm-gateway\litellm
uv sync --locked
```

## Configuration files

- `litellm/models.yaml` is the gateway-owned model and route source.
- `litellm/config.runtime.template.yaml` contains non-generated runtime
  settings.
- `litellm/config.runtime.yaml` is generated and consumed by the routing core
  and both protocol ingress processes.
- `litellm/config.yaml` is used by the conformance test environment only.

The model source intentionally does not set per-model `timeout` or
`stream_timeout`; the old 900-second and 3600-second route limits are not part
of the current policy. The runtime template keeps `num_retries: 0` for general
errors and sets `router_settings.retry_policy.RateLimitErrorRetries: 4` so
only rate-limit failures are retried. LiteLLM uses an upstream `Retry-After`
header when applicable and otherwise uses its own bounded backoff.

Generate and validate the runtime file:

```powershell
Set-Location D:\agent\llm-gateway
pwsh -NoProfile -File .\scripts\generate-runtime-config.ps1
```

Route credentials referenced by `models.yaml` must exist in the environment
of the process that starts the routing core. Secret values are not stored in
the repository, configuration backups, or logs.

## Topology

```text
4100  Unified Responses/Chat protocol ingress
4101  LiteLLM routing core
4102  Chat Completions forwarding hop and direct entry point
```

Port 4100 is the public entry point for both `POST /v1/responses` and
`POST /v1/chat/completions`. Responses requests for chat-mode models are
converted inside 4100 and forwarded to the Chat Completions hop on 4102;
Responses-mode requests go directly to the routing core. Port 4102 also
remains available as a direct Chat Completions entry point. The 4100 ingress
may additionally serve the deployment's `/v1/alpha/*` compatibility
extension; that extension is separate from model routing.

## Startup and stop commands

The existing script filenames are retained because a scheduled task and local
operator tooling reference them. Their implementation is protocol-neutral.

```powershell
Set-Location D:\agent\llm-gateway
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

Startup generates and validates the runtime configuration before replacing
listeners. It then starts the routing core, the unified 4100 ingress, and the
4102 Chat Completions hop, and verifies the unified and chat-only model
catalogs against the generated mode assignments.

## Health endpoints

```text
http://127.0.0.1:4100/health/liveliness
http://127.0.0.1:4101/health/liveliness
http://127.0.0.1:4102/health/liveliness
```

The ingress model catalogs are available at:

```text
http://127.0.0.1:4100/v1/models
http://127.0.0.1:4102/v1/models
```

The 4100 catalog contains the union of all assigned models. The 4102 catalog
contains only models assigned to `chat` in the generated runtime
configuration.

## Verification

```powershell
Set-Location D:\agent\llm-gateway\litellm
uv run pytest -q
```

Do not restart a running deployment merely to inspect this document or the
generated file. Apply a configuration change at a planned operational
boundary.
