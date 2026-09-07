# Runtime configuration rebuild

This document describes how to recreate the local gateway deployment without
including secret values or a model inventory.

## Repository location

```text
D:\agent\litellm-gateway
```

The LiteLLM Python environment is under:

```text
D:\agent\litellm-gateway\litellm\.venv
```

Install or refresh the locked environment with:

```powershell
Set-Location D:\agent\litellm-gateway\litellm
uv sync --locked
```

## Configuration files

- `litellm/models.yaml` is the gateway-owned model and route source.
- `litellm/config.runtime.template.yaml` contains non-generated runtime
  settings.
- `litellm/config.runtime.yaml` is generated and consumed by the routing core
  and both protocol ingress processes.
- `litellm/config.yaml` is used by the conformance test environment only.

Generate and validate the runtime file:

```powershell
Set-Location D:\agent\litellm-gateway
pwsh -NoProfile -File .\scripts\generate-runtime-config.ps1
```

Route credentials referenced by `models.yaml` must exist in the environment
of the process that starts the routing core. Secret values are not stored in
the repository, configuration backups, or logs.

## Topology

```text
4100  Responses protocol ingress
4101  LiteLLM routing core
4102  Chat Completions protocol ingress
```

The two ingress processes forward model traffic to `127.0.0.1:4101`. The
Responses ingress may also serve the deployment's `/v1/alpha/*`
compatibility extension; that extension is separate from model routing.

## Startup and stop commands

The existing script filenames are retained because a scheduled task and local
operator tooling reference them. Their implementation is protocol-neutral.

```powershell
Set-Location D:\agent\litellm-gateway
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

Startup generates and validates the runtime configuration before replacing
listeners. It then starts the routing core and the two protocol ingress
processes, and verifies each protocol model catalog against the generated
mode assignments.

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

Each catalog is filtered by the `mode` field in the generated runtime
configuration.

## Verification

```powershell
Set-Location D:\agent\litellm-gateway\litellm
uv run pytest -q
```

Do not restart a running deployment merely to inspect this document or the
generated file. Apply a configuration change at a planned operational
boundary.
