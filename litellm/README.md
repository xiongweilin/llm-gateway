# Runtime and routing

This directory contains the LiteLLM routing core, its generated runtime
configuration, and the conformance test environment.

## Architecture

```text
Responses client
    -> 4100 Responses protocol ingress
    -> 4101 LiteLLM routing core
    -> upstream

Chat Completions client
    -> 4102 Chat Completions protocol ingress
    -> 4101 LiteLLM routing core
    -> upstream
```

The ingress processes are protocol boundaries. They do not own runtime task
lifecycle, authorization, tool execution, or completion semantics.

## Ports

| Port | Role |
| --- | --- |
| `4100` | Responses protocol ingress |
| `4101` | Internal LiteLLM routing core |
| `4102` | Chat Completions protocol ingress |

All listeners bind to `127.0.0.1` by default.

## Runtime configuration

The gateway-owned source is [models.yaml](models.yaml). It describes runtime
model IDs, protocol modes, upstream route parameters, and environment-variable
references for credentials.

The generated file [config.runtime.yaml](config.runtime.yaml) is produced by:

```powershell
pwsh -NoProfile -File ..\scripts\generate-runtime-config.ps1
```

The template [config.runtime.template.yaml](config.runtime.template.yaml)
contains settings outside the generated model list. Do not edit the generated
model block by hand.

The separate `config.yaml` remains the conformance-test configuration. It is
not the source for the long-running three-port topology.

## Model routing

Each runtime model declares one protocol mode:

- `responses`
- `chat`

The ingress model catalogs are derived from that mode. A model assigned to one
mode is not exposed by the other protocol ingress.

## Protocol ingress

The Responses ingress accepts `POST /v1/responses`, `GET /v1/models`, and the
local health endpoint. It also exposes the narrowly scoped `/v1/alpha/*`
compatibility extension required by the deployment.

The Chat Completions ingress accepts `GET` or `POST /v1/chat/completions`,
`GET /v1/models`, and the local health endpoint. It rejects Responses paths and
the compatibility extension.

Both ingress processes forward model traffic to the routing core. Responses
transport handling includes request decompression, streaming passthrough, and
non-streaming SSE aggregation where required by the client contract.

## Compatibility behavior

Protocol ingress may contain narrowly scoped compatibility transforms for
request shapes or upstream behaviors not handled natively by LiteLLM.

Compatibility transforms must:

- be conditionally triggered by the actual request shape or route;
- leave unrelated requests unchanged;
- remain isolated from protocol routing semantics.

Provider credentials referenced by the runtime model configuration are read
from the process environment. No credential value belongs in this repository.

## Operations

Install the locked environment:

```powershell
Set-Location D:\agent\litellm-gateway\litellm
uv sync --locked
```

The existing lifecycle script paths are retained for scheduled-task
compatibility:

```powershell
Set-Location D:\agent\litellm-gateway
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
pwsh -NoProfile -File .\scripts\stop-agent-gateway.ps1
```

Starting or stopping replaces listeners on ports `4100`, `4101`, and `4102`.
Run those commands only at a safe operational boundary. The start operation
generates and validates `config.runtime.yaml` before touching live processes.

Health endpoints:

```text
http://127.0.0.1:4100/health/liveliness
http://127.0.0.1:4101/health/liveliness
http://127.0.0.1:4102/health/liveliness
```

## Testing

Run the focused unit and conformance tests from `litellm`:

```powershell
Set-Location D:\agent\litellm-gateway\litellm
uv run pytest -q
```

The conformance environment uses a local fake upstream and temporary ports;
it does not require production credentials.

## Security

Keep all listeners on loopback unless an explicit deployment boundary provides
authentication and network isolation. Disable request and response body
logging for credentials and user content. Supply all upstream credentials via
environment variables and do not print their values.
