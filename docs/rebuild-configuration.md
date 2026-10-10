# Rebuild and run

## Configuration owners

- `config/gateway.json`: bind host, service ports, model-route file, and the
  optional Codex control-plane endpoint.
- `core/models.yaml`: public model ids, protocol modes, provider model ids,
  provider endpoints, and credential environment-variable names.
- Provider credential values are supplied through the process environment;
  they do not belong in tracked files.

## Runtime topology

| Port | Service | Responsibility |
|---:|---|---|
| 4100 | Core | Provider route selection and direct outbound model requests |
| 4101 | Unified Agent entry | Shared protocol ingress, dispatch, and adaptation for Codex, agents, and Anthropic clients |
| 4102 | Responses service | Responses-specific protocol handling and compatibility |
| 4103 | Chat service | Chat Completions protocol forwarding and streaming |
| 4104 | Anthropic Messages service | `/v1/messages` and `/v1/messages/count_tokens` forwarding |

Codex, agent, and Anthropic clients connect to 4101. Native Responses requests
go to 4102, Chat Completions requests go to 4103, and Anthropic Messages
requests go to 4104; all three protocol services forward to Core at 4100. For
models marked `codex_responses: true`, 4101 converts Codex Responses requests
to Anthropic Messages and sends them to 4104. The configuration file owns the
port assignments; scripts do not contain a second port map.

## Install and start

From the repository root:

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

The launcher resolves paths relative to its own location. It starts Core,
Chat, Responses, Anthropic Messages, and the Agent entry in dependency order,
then checks health and model catalogs. It does not restart anything when this
document is edited; run the launcher explicitly when ready to apply code changes.

## Health and catalogs

- Core health: `http://127.0.0.1:4100/health/liveliness`
- Agent entry health: `http://127.0.0.1:4101/health/liveliness`
- Responses health: `http://127.0.0.1:4102/health/liveliness`
- Chat service health: `http://127.0.0.1:4103/health/liveliness`
- Anthropic Messages health: `http://127.0.0.1:4104/health/liveliness`
- Unified model catalog: `http://127.0.0.1:4101/v1/models`
- Responses catalog: `http://127.0.0.1:4102/v1/models`
- Chat catalog: `http://127.0.0.1:4103/v1/models`
- Anthropic Messages catalog: `http://127.0.0.1:4104/v1/models`

The 4101 catalog contains native Responses, Chat, and Messages models marked
Codex-compatible. The 4102, 4103, and 4104 catalogs contain models configured
for their respective protocols.
