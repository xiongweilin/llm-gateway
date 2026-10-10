# 重建与运行

## 配置所有者

- `config/gateway.json`：绑定 host、服务端口、模型路由文件，以及可选的 Codex control-plane endpoint。
- `core/models.yaml`：公开 model id、协议模式、provider model id、provider endpoint，以及凭据环境变量名称。
- Provider 凭据值通过进程环境提供；这些值不应进入受版本控制的文件。

## 运行时拓扑

| 端口 | 服务 | 职责 |
|---:|---|---|
| 4100 | Core | 选择 provider 路由并直接发出模型请求 |
| 4101 | 统一 Agent 入口 | Codex、Agent 与 Anthropic 客户端共用的协议入口、分发与适配 |
| 4102 | Responses 服务 | Responses 专属协议处理和兼容 |
| 4103 | Chat 服务 | Chat Completions 协议转发和流式传输 |
| 4104 | Anthropic Messages 服务 | `/v1/messages` 与 `/v1/messages/count_tokens` 协议转发 |

Codex、Agent 和 Anthropic 客户端都连接 4101。原生 Responses 请求进入 4102，Chat Completions 请求进入 4103，Anthropic Messages 请求进入 4104；三个协议服务都会转发到 4100 的 Core。对标记 `codex_responses: true` 的模型，4101 会把 Codex Responses 请求转换成 Anthropic Messages 后发往 4104。端口分配由配置文件统一持有；脚本中不存在第二份端口映射。

## 安装与启动

在仓库根目录执行：

```powershell
uv sync --project core --locked --group dev
pwsh -NoProfile -File .\scripts\start-agent-gateway.ps1
```

launcher 会相对自身位置解析路径，按照依赖顺序启动 Core、Chat、Responses、Anthropic Messages 和 Agent entry，然后检查健康状态和模型目录。编辑本文档不会触发任何重启；准备应用代码变更时需要显式运行 launcher。

## 健康检查与目录

- Core health：`http://127.0.0.1:4100/health/liveliness`
- Agent entry health：`http://127.0.0.1:4101/health/liveliness`
- Responses health：`http://127.0.0.1:4102/health/liveliness`
- Chat service health：`http://127.0.0.1:4103/health/liveliness`
- Anthropic Messages health：`http://127.0.0.1:4104/health/liveliness`
- Unified model catalog：`http://127.0.0.1:4101/v1/models`
- Responses catalog：`http://127.0.0.1:4102/v1/models`
- Chat catalog：`http://127.0.0.1:4103/v1/models`
- Anthropic Messages catalog：`http://127.0.0.1:4104/v1/models`

4101 统一目录包含原生 Responses、Chat，以及标记为 Codex-compatible 的 Messages 模型。4102、4103、4104 的目录分别只包含各自协议服务的模型。
