"""Codex → LiteLLM 传输桥：解压 codex 的 zstd 请求体后转发到后端。

背景：codex CLI/桌面端把 /v1/responses 请求体以 `Content-Encoding: zstd`
发送；LiteLLM(FastAPI) 不解压请求体，导致 model 字段解析失败（400
model=None）。本代理在 127.0.0.1:4100 收请求，zstd 解压后转发到后端
LiteLLM（默认 127.0.0.1:4101），并流式回传 SSE 响应。

安全：仅绑定 loopback；不解析/不记录请求与响应内容；不含任何密钥。
"""
import asyncio
import json
import logging
import math
import sys
from collections import Counter

import aiohttp
import aiohttp.web
import zstandard

log = logging.getLogger("agent-zstd-proxy")

# 低于 opencode-go 模型 1,048,576 token 上下文上限，给输出保留余量。
# 这里只做异常请求的最后保护；正常增长与压缩由 Codex 自身管理。
INPUT_TOKEN_BUDGET = 950_000
PLAINTEXT_COLLABORATION_TOOLS = {"spawn_agent", "send_message", "followup_task"}
COLLABORATION_TOOLS = PLAINTEXT_COLLABORATION_TOOLS | {
    "interrupt_agent",
    "list_agents",
    "wait_agent",
}
PLAINTEXT_COLLABORATION_NAMESPACE = "local_collaboration"

# Responses input item types for tool calls and their outputs. Truncation must
# never leave an output whose tool call was dropped: upstream providers (e.g.
# OpenCode Go) reject such inputs with "No tool call found for tool output".
TOOL_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call"}
TOOL_OUTPUT_TYPES = {
    "custom_tool_call_output",
    "function_call_output",
    "local_shell_call_output",
}
TOOL_NAME_INPUT_TYPES = TOOL_CALL_TYPES | {"tool_search_call", "web_search_call"}


def _json_tokens(value) -> int:
    """Conservative request-size estimate without logging request content."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return max(1, (len(encoded) + 2) // 3)


def request_summary(body: bytes) -> dict[str, object]:
    """Return content-free request metrics safe for local diagnostics."""
    try:
        obj = json.loads(body)
    except Exception:
        return {"body_bytes": len(body), "json": False}

    inp = obj.get("input")
    items = inp if isinstance(inp, list) else []
    roles: Counter[str] = Counter()
    types: Counter[str] = Counter()
    type_bytes: Counter[str] = Counter()
    input_call_names: Counter[str] = Counter()
    agent_content_types: Counter[str] = Counter()
    largest_items = []
    for item in items:
        if not isinstance(item, dict):
            types[type(item).__name__] += 1
            continue
        role = item.get("role")
        item_type = item.get("type")
        item_type_name = str(item_type or "message")
        item_bytes = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
        roles[str(role or "none")] += 1
        types[item_type_name] += 1
        type_bytes[item_type_name] += item_bytes
        if item_type in TOOL_NAME_INPUT_TYPES:
            name = item.get("name")
            namespace = item.get("namespace")
            if isinstance(namespace, str) and namespace and isinstance(name, str) and name:
                display_name = f"{namespace}.{name}"
            else:
                display_name = name if isinstance(name, str) and name else "<none>"
            input_call_names[f"{item_type}:{display_name}"] += 1
        largest_items.append((item_bytes, item_type_name))
        if item_type == "agent_message":
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    part_type = part.get("type") if isinstance(part, dict) else None
                    agent_content_types[str(part_type or "unknown")] += 1

    instructions = obj.get("instructions")
    tools = obj.get("tools")
    tool_declaration_types: Counter[str] = Counter()
    tool_declaration_names: Counter[str] = Counter()

    def visit_tool_declaration(value) -> None:
        if not isinstance(value, dict):
            return
        declaration_type = value.get("type")
        if isinstance(declaration_type, str):
            tool_declaration_types[declaration_type] += 1
            name = value.get("name")
            if isinstance(name, str) and name:
                tool_declaration_names[f"{declaration_type}:{name}"] += 1
        children = value.get("tools")
        if isinstance(children, list):
            for child in children:
                visit_tool_declaration(child)

    if isinstance(tools, list):
        for tool in tools:
            visit_tool_declaration(tool)
    return {
        "body_bytes": len(body),
        "estimated_tokens": _json_tokens(obj),
        "model": obj.get("model"),
        "input_items": len(items),
        "input_roles": dict(sorted(roles.items())),
        "input_types": dict(sorted(types.items())),
        "input_call_names": dict(sorted(input_call_names.items())),
        "input_type_bytes": dict(sorted(type_bytes.items())),
        "largest_input_items": [
            {"bytes": size, "type": item_type}
            for size, item_type in sorted(largest_items, reverse=True)[:5]
        ],
        "agent_message_content_types": dict(sorted(agent_content_types.items())),
        "instructions_chars": len(instructions) if isinstance(instructions, str) else 0,
        "tools_count": len(tools) if isinstance(tools, list) else 0,
        "tools_estimated_tokens": _json_tokens(tools) if isinstance(tools, list) else 0,
        "tool_declaration_types": dict(sorted(tool_declaration_types.items())),
        "tool_declaration_names": dict(sorted(tool_declaration_names.items())),
    }


def _item_tokens(item) -> int:
    return _json_tokens(item)


def _tool_call_id(item) -> str | None:
    """Extract the tool-call identifier from a Responses input item.

    ``function_call``/``function_call_output`` pair on ``call_id``; the
    custom-tool variants pair on ``custom_tool_call_id``/``id``; local-shell
    items use ``call_id``. Checking all three keeps pairing consistent across
    item kinds, and never inspects output content.
    """
    for key in ("call_id", "custom_tool_call_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def normalize_agent_messages(body: bytes) -> bytes:
    """Map Codex-only plaintext agent messages to standard Responses input.

    Multi-agent v2 delivers a delegated task as ``type=agent_message``. The
    OpenAI backend understands that private item type, while OpenCode Go
    currently ignores it. Convert only that route; ChatGPT keeps the native
    item. Encrypted parts remain untouched because only the originating OpenAI
    backend can decrypt them; the response bridge separately forces local
    collaboration calls to use their already-present plaintext arguments.
    """
    try:
        obj = json.loads(body)
    except Exception:
        return body
    inp = obj.get("input")
    model = obj.get("model")
    if not isinstance(inp, list) or not isinstance(model, str) or not model.startswith("opencode-go/"):
        return body

    converted = 0
    invalid = 0
    normalized = []
    for item in inp:
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            normalized.append(item)
            continue
        content = item.get("content")
        if not isinstance(content, list) or not content:
            invalid += 1
            normalized.append(item)
            continue
        plaintext = all(
            isinstance(part, dict)
            and part.get("type") == "input_text"
            and isinstance(part.get("text"), str)
            for part in content
        )
        if not plaintext:
            invalid += 1
            normalized.append(item)
            continue
        normalized.append({
            "type": "message",
            "role": "user",
            "content": content,
        })
        converted += 1

    if not converted:
        if invalid:
            log.warning(
                "agent_message compatibility skipped: invalid=%d",
                invalid,
            )
        return body
    obj["input"] = normalized
    log.info(
        "agent_message compatibility: converted=%d invalid=%d",
        converted,
        invalid,
    )
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def normalize_tool_schemas(body: bytes) -> bytes:
    """Normalize function tool schemas for strict OpenCode Go validation.

    Codex Desktop's automation tools (e.g. ``automation_update``) may carry a
    ``parameters``/``input_schema`` whose top-level ``type`` is null. OpenAI
    accepts that, while OpenCode Go validates strictly and rejects the whole
    request with ``invalid_request_error``. OpenCode Go also requires every
    property name to be present in the schema's ``required`` array. Normalize
    those two compatibility differences only for ``opencode-go/*`` requests;
    ChatGPT requests are left unchanged.
    """
    try:
        obj = json.loads(body)
    except Exception:
        return body

    model = obj.get("model")
    strict_required = isinstance(model, str) and model.startswith("opencode-go/")
    fixed_type = 0
    fixed_required = 0

    def normalize_schema(schema: dict) -> None:
        nonlocal fixed_type, fixed_required

        if schema.get("type") is None:
            schema["type"] = "object"
            fixed_type += 1

        properties = schema.get("properties")
        if isinstance(properties, dict):
            if strict_required:
                required = schema.get("required")
                normalized_required = (
                    [name for name in required if isinstance(name, str)]
                    if isinstance(required, list)
                    else []
                )
                for name in properties:
                    if name not in normalized_required:
                        normalized_required.append(name)
                if normalized_required != required:
                    schema["required"] = normalized_required
                    fixed_required += 1
            for child in properties.values():
                if isinstance(child, dict):
                    normalize_schema(child)

        for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
            child = schema.get(key)
            if isinstance(child, dict):
                normalize_schema(child)
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            children = schema.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        normalize_schema(child)

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") == "function" and isinstance(value.get("name"), str):
            schema = value.get("parameters")
            if not isinstance(schema, dict):
                schema = value.get("input_schema")
            if isinstance(schema, dict):
                normalize_schema(schema)
        for child in value.values():
            visit(child)

    visit(obj)
    if not (fixed_type or fixed_required):
        return body
    log.info(
        "tool schema compatibility: fixed_type=%d fixed_required=%d",
        fixed_type,
        fixed_required,
    )
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def drop_opencode_custom_tools(body: bytes) -> bytes:
    """Remove Responses ``custom`` tool declarations unsupported by OpenCode Go.

    This intentionally leaves ``custom_tool_call`` history items untouched;
    only declarations inside ``tools`` arrays are removed. ChatGPT requests
    and all other tool types pass through unchanged.
    """
    try:
        obj = json.loads(body)
    except Exception:
        return body

    model = obj.get("model")
    if not isinstance(model, str) or not model.startswith("opencode-go/"):
        return body

    dropped = 0

    def visit(value) -> None:
        nonlocal dropped
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, child in list(value.items()):
            if key == "tools" and isinstance(child, list):
                kept = []
                for item in child:
                    if isinstance(item, dict) and item.get("type") == "custom":
                        dropped += 1
                        continue
                    kept.append(item)
                    visit(item)
                value[key] = kept
            else:
                visit(child)

    visit(obj)
    if not dropped:
        return body
    log.info("OpenCode Go custom tool compatibility: dropped=%d", dropped)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def adapt_collaboration_request(body: bytes) -> bytes:
    """Alias reserved collaboration tools so ChatGPT returns plaintext args."""
    try:
        obj = json.loads(body)
    except Exception:
        return body

    aliased_namespaces = 0
    aliased_calls = 0
    stripped_markers = 0

    def visit(value) -> None:
        nonlocal aliased_namespaces, aliased_calls, stripped_markers
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if (
            value.get("type") == "namespace"
            and value.get("name") == "collaboration"
            and isinstance(value.get("tools"), list)
        ):
            value["name"] = PLAINTEXT_COLLABORATION_NAMESPACE
            aliased_namespaces += 1
        call_name = value.get("name")
        flat_call_name = _flat_collaboration_tool_name(call_name)
        if value.get("type") == "function_call" and flat_call_name:
            value["name"] = flat_call_name
            value["namespace"] = "collaboration"
            call_name = flat_call_name
        if (
            value.get("type") == "function_call"
            and not value.get("namespace")
            and isinstance(call_name, str)
            and call_name in COLLABORATION_TOOLS
        ):
            # Some OpenCode Go responses flatten a namespaced call all the
            # way to ``name=spawn_agent``.  Restore the namespace before the
            # history is sent back upstream, otherwise Codex cannot match it
            # to the collaboration handler on the next turn.
            value["namespace"] = "collaboration"
        if (
            value.get("type") == "function_call"
            and value.get("namespace") == "collaboration"
            and isinstance(call_name, str)
            and call_name in COLLABORATION_TOOLS
        ):
            value["namespace"] = PLAINTEXT_COLLABORATION_NAMESPACE
            value.pop("encrypted_function_args", None)
            aliased_calls += 1
        tool_name = value.get("name")
        if isinstance(tool_name, str) and tool_name in PLAINTEXT_COLLABORATION_TOOLS:
            parameters = value.get("parameters") or value.get("input_schema")
            if isinstance(parameters, dict):
                properties = parameters.get("properties")
                message = properties.get("message") if isinstance(properties, dict) else None
                if isinstance(message, dict) and message.pop("encrypted", None) is not None:
                    stripped_markers += 1
        for child in value.values():
            visit(child)

    visit(obj)
    if not (aliased_namespaces or aliased_calls or stripped_markers):
        return body
    log.info(
        "collaboration request compatibility: aliased_namespaces=%d aliased_calls=%d stripped_encryption_markers=%d",
        aliased_namespaces,
        aliased_calls,
        stripped_markers,
    )
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def _flat_collaboration_tool_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    for prefix in ("local_collaboration.", "collaboration.", "local_collaboration__", "collaboration__"):
        tool_name = name.removeprefix(prefix)
        if tool_name != name and tool_name in COLLABORATION_TOOLS:
            return tool_name
    return None


def _force_plaintext_collaboration_calls(value) -> int:
    """Mark Codex collaboration calls as plaintext without logging arguments."""
    changed = 0
    if isinstance(value, list):
        for item in value:
            changed += _force_plaintext_collaboration_calls(item)
        return changed
    if not isinstance(value, dict):
        return 0

    if value.get("type") == "function_call":
        flat_name = _flat_collaboration_tool_name(value.get("name"))
        if flat_name:
            value["name"] = flat_name
            value["namespace"] = "collaboration"
        call_name = value.get("name")
        namespace = value.get("namespace")
        if (
            not namespace
            and isinstance(call_name, str)
            and call_name in COLLABORATION_TOOLS
        ):
            # OpenCode Go may omit both the namespace field and the dotted
            # prefix.  These names are reserved for collaboration tools in
            # Codex, so infer the namespace before returning the response.
            value["namespace"] = "collaboration"
            namespace = "collaboration"
    else:
        call_name = None
        namespace = None
    if call_name in COLLABORATION_TOOLS:
        if namespace == PLAINTEXT_COLLABORATION_NAMESPACE:
            value["namespace"] = "collaboration"
            if call_name in PLAINTEXT_COLLABORATION_TOOLS:
                value["encrypted_function_args"] = []
            changed += 1
        elif namespace == "collaboration" and value.get("encrypted_function_args") != []:
            if call_name in PLAINTEXT_COLLABORATION_TOOLS:
                value["encrypted_function_args"] = []
            changed += 1
    for child in value.values():
        changed += _force_plaintext_collaboration_calls(child)
    return changed


def _coerce_integral_float_numbers(value):
    """Convert JSON floats representing integers to JSON integers recursively."""
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value), True
        return value, False
    if isinstance(value, list):
        normalized = []
        changed = False
        for item in value:
            child, child_changed = _coerce_integral_float_numbers(item)
            normalized.append(child)
            changed = changed or child_changed
        return normalized, changed
    if isinstance(value, dict):
        normalized = {}
        changed = False
        for key, child in value.items():
            normalized_child, child_changed = _coerce_integral_float_numbers(child)
            normalized[key] = normalized_child
            changed = changed or child_changed
        return normalized, changed
    return value, False


def _normalize_function_call_arguments(value) -> int:
    """Normalize integral float literals in Responses ``function_call`` items."""
    changed = 0
    if isinstance(value, list):
        for item in value:
            changed += _normalize_function_call_arguments(item)
        return changed
    if not isinstance(value, dict):
        return 0

    if value.get("type") == "function_call":
        arguments = value.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except Exception:
                parsed = None
            if parsed is not None:
                normalized, args_changed = _coerce_integral_float_numbers(parsed)
                if args_changed:
                    value["arguments"] = json.dumps(
                        normalized,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    changed += 1
        elif isinstance(arguments, (dict, list)):
            normalized, args_changed = _coerce_integral_float_numbers(arguments)
            if args_changed:
                value["arguments"] = normalized
                changed += 1

    for child in value.values():
        changed += _normalize_function_call_arguments(child)
    return changed


def rewrite_sse_collaboration_calls(
    data: bytes,
    final: bool = False,
    normalize_function_args: bool = False,
) -> tuple[bytes, bytes, int]:
    """Rewrite complete SSE data lines and retain any split trailing line."""
    parts = data.split(b"\n")
    pending = b""
    if not final:
        pending = parts.pop()
    output = []
    changed = 0
    for raw_line in parts:
        had_cr = raw_line.endswith(b"\r")
        line = raw_line[:-1] if had_cr else raw_line
        if line.startswith(b"data: ") and line[6:] != b"[DONE]":
            try:
                event = json.loads(line[6:])
            except Exception:
                event = None
            if event is not None:
                event_changed = _force_plaintext_collaboration_calls(event)
                if normalize_function_args:
                    event_changed += _normalize_function_call_arguments(event)
                if event_changed:
                    changed += event_changed
                    line = b"data: " + json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
        output.append(line + (b"\r\n" if had_cr else b"\n"))
    return b"".join(output), pending, changed


def aggregate_responses_sse(
    data: bytes,
    normalize_function_args: bool = False,
) -> tuple[bytes | None, int]:
    """Build one Responses JSON object for callers that requested non-streaming."""
    completed = None
    output_items = {}
    rewritten_calls = 0
    for raw_line in data.splitlines():
        if not raw_line.startswith(b"data: ") or raw_line[6:] == b"[DONE]":
            continue
        try:
            event = json.loads(raw_line[6:])
        except Exception:
            continue
        event_type = event.get("type")
        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            output_index = event.get("output_index")
            if isinstance(output_index, int):
                output_items[output_index] = event["item"]
        elif event_type == "response.completed" and isinstance(event.get("response"), dict):
            completed = event["response"]

    if completed is None:
        return None, 0
    if not completed.get("output") and output_items:
        completed["output"] = [item for _, item in sorted(output_items.items())]
    rewritten_calls = _force_plaintext_collaboration_calls(completed)
    if normalize_function_args:
        rewritten_calls += _normalize_function_call_arguments(completed)
    return (
        json.dumps(completed, ensure_ascii=False, separators=(",", ":")).encode(),
        rewritten_calls,
    )


def truncate_input(body: bytes) -> bytes:
    """超长会话历史截断：保留首条（系统/指令）+ 末尾最近条目，丢最旧。
    仅在超预算时改动；不解析则原样返回。"""
    try:
        obj = json.loads(body)
    except Exception:
        return body
    inp = obj.get("input")
    if not isinstance(inp, list) or len(inp) < 2:
        return body
    envelope = dict(obj)
    envelope["input"] = []
    fixed = _json_tokens(envelope)
    total = fixed + sum(_item_tokens(i) for i in inp)
    if total <= INPUT_TOKEN_BUDGET:
        return body
    head = [inp[0]]
    tail = []
    acc = fixed + _item_tokens(head[0])
    for item in reversed(inp[1:]):
        t = _item_tokens(item)
        # Always retain the newest item even if it alone exceeds the guard.
        if tail and acc + t > INPUT_TOKEN_BUDGET:
            break
        tail.append(item)
        acc += t
    # Reconcile tool call/output pairing after truncation. A dropped tool call
    # can orphan its output anywhere in the kept tail (parallel tool rounds
    # interleave calls and outputs, so the orphan may sit behind a kept call
    # instead of at the old boundary), so match by call id instead of only
    # trimming outputs at the tail edge. Dropping an output whose call was kept
    # is never done here: call-without-output is a legitimate in-flight state,
    # while output-without-call is rejected by strict upstream providers.
    kept = head + list(reversed(tail))
    call_ids = {
        _tool_call_id(item)
        for item in kept
        if isinstance(item, dict)
        and item.get("type") in TOOL_CALL_TYPES
        and _tool_call_id(item) is not None
    }
    reconciled = []
    for item in kept:
        if (
            isinstance(item, dict)
            and item.get("type") in TOOL_OUTPUT_TYPES
            and _tool_call_id(item) not in call_ids
        ):
            acc -= _item_tokens(item)
            continue
        reconciled.append(item)
    kept = reconciled
    log.warning(
        "context truncation: items %d->%d tokens %d->%d",
        len(inp), len(kept), total, acc,
    )
    obj["input"] = kept
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def decompress(body: bytes, encoding: str) -> bytes:
    # aiohttp 3.14 已按 Content-Encoding 自动解压请求体（实测 zstd 也被
    # 解压成 JSON）。仅当 body 仍以 zstd magic 开头时手动解压，作为
    # 未来行为差异的防御。
    if body[:4] == b"\x28\xb5\x2f\xfd":
        dctx = zstandard.ZstdDecompressor()
        dobj = dctx.decompressobj()
        out = dobj.decompress(body)
        out += dobj.flush()
        return out
    enc = (encoding or "").lower()
    return body


def normalize_scalar_responses_input(body: bytes) -> bytes:
    """Convert Codex's scalar Responses input to the gateway's list shape.

    The ChatGPT Codex backend rejects a scalar ``input`` with ``Input must be a
    list`` even though the public Responses contract permits a string. Keep
    the conversion content-preserving and avoid logging the prompt.
    """
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return body
    value = obj.get("input")
    if not isinstance(value, str):
        return body
    obj["input"] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": value}],
        }
    ]
    log.info("Responses compatibility: normalized scalar input to one user message")
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


async def handle(req: aiohttp.web.Request, backend: str, session: aiohttp.ClientSession):
    # Codex 对 /v1/responses 总是先尝试 WebSocket 传输；本代理对 WS 返回
    # 426 UPGRADE_REQUIRED，Codex 据此干净回退到 HTTP（FallbackToHttp）。
    # 返回 404/405 会让桌面端进入重连循环。
    if (
        req.headers.get("Upgrade", "").lower() == "websocket"
        and "/responses" in req.path
    ):
        return aiohttp.web.Response(
            status=426,
            content_type="application/json",
            text='{"error":{"type":"upgrade_required",'
            '"message":"Responses WebSocket transport is disabled; use HTTP"}}',
        )
    # GET /v1/responses（会话打开时的探针/取响应）返回 404 而不是
    # LiteLLM 的 405——桌面端把 405 视为「不支持 Responses 协议」并放弃，
    # 404 则视为「无此响应」并继续走 POST。
    if req.method == "GET" and "/responses" in req.path:
        return aiohttp.web.Response(
            status=404,
            content_type="application/json",
            text='{"error":{"type":"not_found","message":"response not found"}}',
        )
    body = await req.read()
    enc = req.headers.get("Content-Encoding", "")
    dec = decompress(body, enc)
    caller_stream = True
    normalize_function_args = False
    if req.method == "POST" and "/responses" in req.path:
        try:
            request_obj = json.loads(dec)
            caller_stream = bool(request_obj.get("stream", False))
            model = request_obj.get("model")
            normalize_function_args = (
                isinstance(model, str) and model.startswith("opencode-go/")
            )
        except Exception:
            pass
        before = request_summary(dec)
        log.info("request summary %s", json.dumps(before, sort_keys=True))
        dec = normalize_scalar_responses_input(dec)
        dec = adapt_collaboration_request(dec)
        dec = normalize_agent_messages(dec)
        dec = drop_opencode_custom_tools(dec)
        dec = normalize_tool_schemas(dec)
        normalized = request_summary(dec)
        if normalized.get("input_types") != before.get("input_types"):
            log.info("request summary after compatibility %s", json.dumps(normalized, sort_keys=True))
        before_len = len(dec)
        dec = truncate_input(dec)
        if len(dec) != before_len:
            after = request_summary(dec)
            log.warning("request summary after truncation %s", json.dumps(after, sort_keys=True))

    headers = {}
    for k, v in req.headers.items():
        lk = k.lower()
        if lk in ("host", "content-length", "content-encoding", "connection", "transfer-encoding"):
            continue
        headers[k] = v
    headers["Content-Length"] = str(len(dec))

    url = f"{backend}{req.path}"
    if req.query_string:
        url += f"?{req.query_string}"
    try:
        async with session.request(
            req.method, url, data=dec, headers=headers,
            # A Codex CLI session can make multiple Responses turns. The
            # proxy must outlive the control-plane session budget so it does
            # not cancel a still-progressing upstream request first.
            compress=False, timeout=aiohttp.ClientTimeout(total=1200),
        ) as up:
            upstream_is_sse = "text/event-stream" in up.headers.get("Content-Type", "").lower()
            if req.method == "POST" and "/responses" in req.path and not caller_stream and upstream_is_sse:
                upstream_body = await up.read()
                aggregated, rewritten_calls = aggregate_responses_sse(
                    upstream_body,
                    normalize_function_args=normalize_function_args,
                )
                if aggregated is not None:
                    headers = {
                        k: v
                        for k, v in up.headers.items()
                        if k.lower()
                        not in {
                            "content-length",
                            "content-type",
                            "transfer-encoding",
                            "connection",
                        }
                    }
                    if rewritten_calls:
                        log.info(
                            "response compatibility: normalized_function_args_or_collaboration=%d",
                            rewritten_calls,
                        )
                    log.info("responses transport compatibility: aggregated_sse_bytes=%d", len(upstream_body))
                    return aiohttp.web.Response(
                        status=up.status,
                        body=aggregated,
                        headers=headers,
                        content_type="application/json",
                    )
            resp = aiohttp.web.StreamResponse(status=up.status)
            for k, v in up.headers.items():
                lk = k.lower()
                if lk in ("content-length", "transfer-encoding", "connection"):
                    continue
                resp.headers[k] = v
            await resp.prepare(req)
            rewrite_collaboration = (
                "/responses" in req.path
                and upstream_is_sse
            )
            pending = b""
            rewritten_calls = 0
            async for chunk in up.content.iter_any():
                if chunk:
                    if rewrite_collaboration:
                        out, pending, changed = rewrite_sse_collaboration_calls(
                            pending + chunk,
                            normalize_function_args=normalize_function_args,
                        )
                        rewritten_calls += changed
                        if out:
                            await resp.write(out)
                    else:
                        await resp.write(chunk)
            if rewrite_collaboration and pending:
                out, _, changed = rewrite_sse_collaboration_calls(
                    pending,
                    final=True,
                    normalize_function_args=normalize_function_args,
                )
                rewritten_calls += changed
                if out:
                    await resp.write(out)
            if rewritten_calls:
                log.info(
                    "response compatibility: normalized_function_args_or_collaboration=%d",
                    rewritten_calls,
                )
            await resp.write_eof()
            return resp
    except Exception as exc:  # noqa: BLE001
        log.warning("backend request failed: %s", exc)
        return aiohttp.web.Response(status=502, text="agent-zstd-proxy: backend unreachable")


async def main() -> None:
    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 4100
    backend = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:4101"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # 桌面端打开会话会发送完整历史（+工具 schema），超过 aiohttp 默认
    # 1MB 请求体上限会返回 413；调大至 128MB。
    app = aiohttp.web.Application(client_max_size=128 * 1024 * 1024)
    connector = aiohttp.TCPConnector(limit=64)
    session = aiohttp.ClientSession(connector=connector)
    app.router.add_route("*", "/{tail:.*}", lambda r: handle(r, backend, session))
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", listen_port)
    await site.start()
    log.info("agent-zstd-proxy listening on 127.0.0.1:%s -> %s", listen_port, backend)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
