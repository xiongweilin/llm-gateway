# Runtime and routing

This directory contains the LiteLLM routing core, its generated runtime
configuration, and the conformance test environment.

## Architecture

```text
Responses or Chat client
    -> 4100 unified protocol ingress
    -> 4101 LiteLLM routing core
    -> upstream

4100 Responses request for an ordinary chat-mode model
    -> 4102 Chat Completions forwarding hop
    -> 4101 LiteLLM routing core
    -> upstream

Union Alpha Free Responses request
    -> 4100 native Anthropic Messages bridge
    -> OpenCode Go /v1/messages
```

The 4100 ingress is the public protocol boundary. The 4102 process remains a
separate Chat Completions transport and direct entry point. Neither ingress
owns runtime task lifecycle, authorization, tool execution, or completion
semantics.

## Ports

| Port | Role |
| --- | --- |
| `4100` | Unified Responses/Chat protocol ingress |
| `4101` | Internal LiteLLM routing core |
| `4102` | Chat Completions forwarding hop and direct entry point |

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

### Retry and timeout policy

`models.yaml` does not set per-model `timeout` or `stream_timeout`. The
previous 900-second GPT and 3600-second Muse/DeepSeek/Omen limits are therefore not
declared by this gateway; LiteLLM may still apply its package-level fallback
where the underlying request path requires one.

The generated runtime template leaves general router retries at zero and
enables four retries only for `RateLimitError`. LiteLLM uses the upstream
`Retry-After` header when it contains a usable short delay, otherwise it uses
its bounded exponential backoff. Other error classes remain outside this
retry policy.

## Model routing

Each runtime model declares one protocol mode:

- `responses`
- `chat`

The 4100 catalog contains the union of both modes. The 4102 catalog contains
only `chat` models. The mode also selects the internal upstream protocol when
4100 receives a Responses request.

## Protocol ingress

The unified 4100 ingress accepts `POST /v1/responses` and
`POST /v1/chat/completions`, plus `GET /v1/models` and the local health endpoint. It also
exposes the narrowly scoped `/v1/alpha/*` compatibility extension required by
the deployment. Responses requests for ordinary chat-mode models are converted
to Chat Completions and sent through 4102. Union Alpha Free is the deliberate
exception: its Responses request is converted directly to the Anthropic
Messages shape and sent to the provider's `/v1/messages` route, with a
bounded Union-only transient retry and Responses-compatible SSE conversion.

The 4102 Chat Completions process accepts `GET` or `POST /v1/chat/completions`,
plus `GET /v1/models` and the local health endpoint. It
rejects Responses paths and the compatibility extension.

The two transport processes forward model traffic to the routing core.
Responses transport handling includes request decompression, streaming
passthrough, non-streaming SSE aggregation, and the Responses-to-Chat
conversion required by the unified 4100 entry point.

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
Set-Location D:\agent\llm-gateway\litellm
uv sync --locked
```

The existing lifecycle script paths are retained for scheduled-task
compatibility:

```powershell
Set-Location D:\agent\llm-gateway
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
Set-Location D:\agent\llm-gateway\litellm
uv run pytest -q
```

The conformance environment uses a local fake upstream and temporary ports;
it does not require production credentials.

## Security

Keep all listeners on loopback unless an explicit deployment boundary provides
authentication and network isolation. Disable request and response body
logging for credentials and user content. Supply all upstream credentials via
environment variables and do not print their values.
