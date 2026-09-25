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
| 4101 | Unified Agent entry | Stable agent-facing endpoint for model protocols |
| 4102 | Responses service | Responses-specific protocol handling and compatibility |
| 4103 | Chat service | Chat Completions protocol forwarding and streaming |

Agent clients connect to 4101. Responses requests go to 4102 and Chat
Completions requests go to 4103; both protocol services forward to Core at
4100. Responses-to-Chat conversion is not performed. The configuration file
owns the port assignments; scripts do not contain a second port map.

## Install and start

From the repository root:

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

The launcher resolves paths relative to its own location. It starts Core,
Chat, Responses, and the Agent entry in dependency order, then checks health
and model catalogs. It does not restart anything when this document is edited;
run the launcher explicitly when ready to apply code changes.

## Health and catalogs

- Core health: `http://127.0.0.1:4100/health/liveliness`
- Agent entry health: `http://127.0.0.1:4101/health/liveliness`
- Responses health: `http://127.0.0.1:4102/health/liveliness`
- Chat service health: `http://127.0.0.1:4103/health/liveliness`
- Unified model catalog: `http://127.0.0.1:4101/v1/models`
- Responses catalog: `http://127.0.0.1:4102/v1/models`
- Chat catalog: `http://127.0.0.1:4103/v1/models`

The unified catalog contains every configured route. The Responses and Chat
catalogs contain only models configured for their respective protocol.
