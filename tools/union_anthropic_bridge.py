"""Native Anthropic Messages transport for the Union Alpha Free candidate.

The public gateway speaks OpenAI Responses, while the Union deployment is an
Anthropic Messages endpoint.  This module keeps that exception isolated from
the normal LiteLLM route so a provider outage or a provider-specific request
shape cannot change the other model paths.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import aiohttp.web
import yaml

from response_chat_bridge import response_to_sse


log = logging.getLogger("union-anthropic-bridge")

UNION_MODEL_ID = "opencode-go/union-alpha-free"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 32_768
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.5, 1.5)
SESSION_HEADER = "x-opencode-session"
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass(frozen=True)
class UnionAnthropicRoute:
    """The native provider route resolved from the generated runtime config."""

    api_base: str
    model: str
    api_key_env: str


def load_union_anthropic_route(config_path: str) -> UnionAnthropicRoute | None:
    """Load the Union native route without reading any credential value.

    ``models.yaml`` remains the route authority; the runtime YAML is the
    generated file consumed by the running gateway.  Only an Anthropic route
    with an environment-variable key reference is eligible for this path.
    """
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("model_list"), list):
        return None

    for entry in data["model_list"]:
        if not isinstance(entry, dict) or entry.get("model_name") != UNION_MODEL_ID:
            continue
        params = entry.get("litellm_params")
        if not isinstance(params, dict):
            return None
        api_base = params.get("api_base")
        provider_model = params.get("model")
        api_key_ref = params.get("api_key")
        if (
            not isinstance(api_base, str)
            or not api_base.strip()
            or not isinstance(provider_model, str)
            or not provider_model.startswith("anthropic/")
            or not isinstance(api_key_ref, str)
            or not api_key_ref.startswith("os.environ/")
        ):
            return None
        api_key_env = api_key_ref.removeprefix("os.environ/").strip()
        if not api_key_env:
            return None
        return UnionAnthropicRoute(
            api_base=api_base.rstrip("/"),
            model=provider_model.split("/", 1)[1],
            api_key_env=api_key_env,
        )
    return None


def _json_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
    if value is None:
        return {}
    return value


def _safe_tool_id(raw_id: object, id_map: dict[str, str]) -> str:
    original = raw_id if isinstance(raw_id, str) and raw_id else "tool_call"
    existing = id_map.get(original)
    if existing is not None:
        return existing
    candidate = _SAFE_ID_RE.sub("_", original) or "tool_call"
    used = set(id_map.values())
    unique = candidate
    suffix = 2
    while unique in used:
        unique = f"{candidate}_{suffix}"
        suffix += 1
    id_map[original] = unique
    return unique


def _text_block(text: object) -> dict | None:
    if not isinstance(text, str):
        return None
    if not text:
        return None
    return {"type": "text", "text": text}


def _image_block(part: dict) -> dict | None:
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    if not isinstance(image_url, str) or not image_url:
        return None
    if image_url.startswith("data:"):
        header, separator, encoded = image_url.partition(",")
        if not separator:
            return None
        media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        try:
            base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            },
        }
    return {
        "type": "image",
        "source": {"type": "url", "url": image_url},
    }


def _content_blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        block = _text_block(content)
        return [block] if block is not None else []
    if not isinstance(content, list):
        return []

    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            block = _text_block(part.get("text"))
            if block is not None:
                blocks.append(block)
        elif part_type in {"image_url", "input_image", "output_image"}:
            block = _image_block(part)
            if block is not None:
                blocks.append(block)
    return blocks


def _append_native_message(messages: list[dict], role: str, content: list[dict]) -> None:
    if not content:
        content = [{"type": "text", "text": " "}]
    if messages and messages[-1].get("role") == role:
        previous = messages[-1].get("content")
        if isinstance(previous, list):
            previous.extend(content)
            return
    messages.append({"role": role, "content": content})


def _native_messages(
    chat_messages: object,
) -> tuple[list[dict], list[dict], dict[str, str]]:
    messages: list[dict] = []
    system: list[dict] = []
    tool_ids: dict[str, str] = {}
    if not isinstance(chat_messages, list):
        return (
            [{"role": "user", "content": [{"type": "text", "text": " "}]}],
            system,
            tool_ids,
        )

    for message in chat_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            system.extend(_content_blocks(message.get("content")))
            continue
        if role == "tool":
            call_id = _safe_tool_id(message.get("tool_call_id"), tool_ids)
            output = message.get("content", "")
            if not isinstance(output, str):
                output = _json_text(output)
            _append_native_message(
                messages,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": output or " ",
                    }
                ],
            )
            continue
        if role not in {"user", "assistant"}:
            role = "user"

        content = _content_blocks(message.get("content"))
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        function = {}
                    name = function.get("name")
                    if not isinstance(name, str) or not name:
                        name = "tool"
                    content.append(
                        {
                            "type": "tool_use",
                            "id": _safe_tool_id(
                                call.get("id"),
                                tool_ids,
                            ),
                            "name": name,
                            "input": _json_value(function.get("arguments", "")),
                        }
                    )
        _append_native_message(messages, role, content)

    if not messages:
        messages.append({"role": "user", "content": [{"type": "text", "text": " "}]})
    return messages, system, tool_ids


def _native_tools(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        return []
    converted: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = function.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        item = {"name": name, "input_schema": schema}
        description = function.get("description")
        if isinstance(description, str) and description:
            item["description"] = description
        converted.append(item)
    return converted


def build_anthropic_request(
    chat_request: dict,
    route: UnionAnthropicRoute,
) -> tuple[dict, dict[str, str]]:
    """Convert the already-normalized Union Chat request to native Messages."""
    messages, system, tool_ids = _native_messages(chat_request.get("messages"))
    request = {
        "model": route.model,
        "messages": messages,
        "stream": bool(chat_request.get("stream", False)),
    }
    if system:
        request["system"] = system

    tools = _native_tools(chat_request.get("tools"))
    if tools:
        request["tools"] = tools
        tool_choice = chat_request.get("tool_choice")
        if tool_choice == "auto" or tool_choice is None:
            request["tool_choice"] = {"type": "auto"}
        elif tool_choice == "required":
            request["tool_choice"] = {"type": "any"}
        elif isinstance(tool_choice, dict):
            request["tool_choice"] = tool_choice

    for key in ("thinking", "temperature", "top_p"):
        value = chat_request.get(key)
        if value is not None:
            request[key] = value
    stop = chat_request.get("stop")
    if isinstance(stop, str):
        request["stop_sequences"] = [stop]
    elif isinstance(stop, list):
        request["stop_sequences"] = [item for item in stop if isinstance(item, str)]

    max_tokens = chat_request.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    thinking = request.get("thinking")
    if isinstance(thinking, dict):
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and budget >= max_tokens:
            max_tokens = budget + 1
    request["max_tokens"] = max_tokens
    metadata = chat_request.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("user_id"), str):
        request["metadata"] = {"user_id": metadata["user_id"]}
    return request, tool_ids


def _provider_headers(
    route: UnionAnthropicRoute,
    provider_session: str | None,
) -> dict[str, str] | None:
    api_key = os.environ.get(route.api_key_env)
    if not api_key:
        return None
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": api_key,
    }
    if provider_session:
        headers[SESSION_HEADER] = provider_session
    return headers


def _response_usage(usage: object) -> dict:
    source = usage if isinstance(usage, dict) else {}
    input_tokens = source.get("input_tokens", 0)
    output_tokens = source.get("output_tokens", 0)
    input_tokens = input_tokens if isinstance(input_tokens, int) else 0
    output_tokens = output_tokens if isinstance(output_tokens, int) else 0
    cached = source.get("cache_read_input_tokens", 0)
    cached = cached if isinstance(cached, int) else 0
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _restore_tool_name(
    name: object,
    tool_name_map: Mapping[str, tuple[str | None, str]],
) -> tuple[str, str | None]:
    if not isinstance(name, str) or not name:
        return "tool", None
    mapped = tool_name_map.get(name)
    if mapped is None:
        return name, None
    namespace, original = mapped
    return original, namespace


def _response_id(raw_id: object) -> str:
    if isinstance(raw_id, str) and raw_id.startswith("resp_"):
        return raw_id
    if isinstance(raw_id, str) and raw_id:
        return f"resp_{raw_id}"
    return f"resp_{int(time.time() * 1000)}"


def anthropic_response_to_responses(
    provider_response: dict,
    response_model: str,
    tool_name_map: Mapping[str, tuple[str | None, str]] | None = None,
    tool_id_map: Mapping[str, str] | None = None,
) -> dict:
    tool_name_map = tool_name_map or {}
    reverse_tool_ids = {safe: original for original, safe in (tool_id_map or {}).items()}
    response_id = _response_id(provider_response.get("id"))
    output: list[dict] = []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    blocks = provider_response.get("content")
    if not isinstance(blocks, list):
        blocks = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"thinking", "redacted_thinking"}:
            text = block.get("thinking")
            if isinstance(text, str) and text:
                reasoning_parts.append(text)
            continue
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
            continue
        if block_type != "tool_use":
            continue
        raw_id = block.get("id")
        call_id = reverse_tool_ids.get(raw_id, raw_id)
        if not isinstance(call_id, str) or not call_id:
            call_id = f"{response_id}_call_{index}"
        name, namespace = _restore_tool_name(block.get("name"), tool_name_map)
        item = {
            "id": call_id,
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": _json_text(block.get("input", {})),
        }
        if namespace:
            item["namespace"] = namespace
        output.append(item)

    if reasoning_parts:
        output.insert(
            0,
            {
                "id": f"{response_id}_reasoning",
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "".join(reasoning_parts)}
                ],
            },
        )
    if text_parts:
        text = "".join(text_parts)
        message = {
            "id": f"{response_id}_message",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        insert_at = 1 if reasoning_parts else 0
        output.insert(insert_at, message)

    stop_reason = provider_response.get("stop_reason")
    status = "incomplete" if stop_reason == "max_tokens" else "completed"
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": response_model,
        "output": output,
        "usage": _response_usage(provider_response.get("usage")),
    }
    if text_parts:
        response["output_text"] = "".join(text_parts)
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


def _sse_frame(event_type: str, payload: dict) -> bytes:
    event = dict(payload)
    event.setdefault("type", event_type)
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {encoded}\n\n".encode()


def _failure_sse(response_model: str, message: str, response_id: str | None = None) -> bytes:
    response = {
        "id": response_id or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "failed",
        "model": response_model,
        "output": [],
        "error": {"code": "union_upstream_unavailable", "message": message},
    }
    in_progress = dict(response)
    in_progress["status"] = "in_progress"
    in_progress.pop("error", None)
    return b"".join(
        [
            _sse_frame("response.created", {"response": in_progress}),
            _sse_frame("response.in_progress", {"response": in_progress}),
            _sse_frame("response.failed", {"response": response}),
        ]
    )


class AnthropicStreamBridge:
    """Incrementally convert Anthropic Messages SSE to Responses SSE."""

    def __init__(
        self,
        response_model: str,
        tool_name_map: Mapping[str, tuple[str | None, str]] | None = None,
        tool_id_map: Mapping[str, str] | None = None,
        event_rewriter: Callable[[dict], object] | None = None,
    ) -> None:
        self.response_model = response_model
        self.tool_name_map = tool_name_map or {}
        self.reverse_tool_ids = {
            safe: original
            for original, safe in (tool_id_map or {}).items()
        }
        self.event_rewriter = event_rewriter
        self._buffer = b""
        self._response_id = f"resp_{uuid.uuid4().hex}"
        self._created_at = int(time.time())
        self._started = False
        self._finished = False
        self.failed = False
        self._output: list[dict] = []
        self._blocks: dict[int, dict] = {}
        self._usage: dict = {}
        self._stop_reason: str | None = None

    def _frame(self, event_type: str, payload: dict) -> bytes:
        event = dict(payload)
        event.setdefault("type", event_type)
        if self.event_rewriter is not None:
            self.event_rewriter(event)
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {encoded}\n\n".encode()

    def start(self) -> bytes:
        if self._started:
            return b""
        self._started = True
        response = {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": "in_progress",
            "model": self.response_model,
            "output": [],
        }
        return b"".join(
            [
                self._frame("response.created", {"response": response}),
                self._frame("response.in_progress", {"response": response}),
            ]
        )

    def _new_block(self, index: int, block_type: str, block: dict | None = None) -> list[bytes]:
        if index in self._blocks:
            return []
        block = block or {}
        response_id = self._response_id
        if block_type == "text":
            item = {
                "id": f"{response_id}_message_{index}",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
        elif block_type in {"thinking", "redacted_thinking"}:
            item = {
                "id": f"{response_id}_reasoning_{index}",
                "type": "reasoning",
                "summary": [],
            }
        elif block_type == "tool_use":
            raw_name = block.get("name")
            name, namespace = _restore_tool_name(raw_name, self.tool_name_map)
            call_id = self.reverse_tool_ids.get(block.get("id"), block.get("id"))
            if not isinstance(call_id, str) or not call_id:
                call_id = f"{response_id}_call_{index}"
            item = {
                "id": call_id,
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": "",
            }
            if namespace:
                item["namespace"] = namespace
        else:
            return []
        state = {
            "type": block_type,
            "item": item,
            "index": len(self._output),
            "text": "",
            "arguments": "",
            "closed": False,
        }
        self._blocks[index] = state
        self._output.append(item)
        frames = [
            self._frame(
                "response.output_item.added",
                {"output_index": state["index"], "item": item},
            )
        ]
        if block_type == "text":
            frames.append(
                self._frame(
                    "response.content_part.added",
                    {
                        "output_index": state["index"],
                        "content_index": 0,
                        "item_id": item["id"],
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            )
        return frames

    def _close_block(self, index: int) -> list[bytes]:
        state = self._blocks.get(index)
        if state is None or state["closed"]:
            return []
        state["closed"] = True
        item = state["item"]
        output_index = state["index"]
        block_type = state["type"]
        frames: list[bytes] = []
        if block_type == "text":
            item["status"] = "completed"
            item["content"] = [
                {"type": "output_text", "text": state["text"], "annotations": []}
            ]
            frames.extend(
                [
                    self._frame(
                        "response.output_text.done",
                        {
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "text": state["text"],
                        },
                    ),
                    self._frame(
                        "response.content_part.done",
                        {
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "part": item["content"][0],
                        },
                    ),
                ]
            )
        elif block_type in {"thinking", "redacted_thinking"}:
            item["summary"] = [{"type": "summary_text", "text": state["text"]}]
            frames.append(
                self._frame(
                    "response.reasoning_summary_text.done",
                    {
                        "item_id": item["id"],
                        "output_index": output_index,
                        "text": state["text"],
                    },
                )
            )
        elif block_type == "tool_use":
            item["arguments"] = state["arguments"]
            frames.append(
                self._frame(
                    "response.function_call_arguments.done",
                    {
                        "item_id": item["id"],
                        "output_index": output_index,
                        "arguments": state["arguments"],
                    },
                )
            )
        frames.append(
            self._frame(
                "response.output_item.done",
                {"output_index": output_index, "item": item},
            )
        )
        return frames

    def _finish(self) -> list[bytes]:
        if self._finished:
            return []
        self._finished = True
        frames: list[bytes] = []
        for index in list(self._blocks):
            frames.extend(self._close_block(index))
        status = "incomplete" if self._stop_reason == "max_tokens" else "completed"
        response = {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": status,
            "model": self.response_model,
            "output": self._output,
            "usage": _response_usage(self._usage),
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        frames.append(self._frame("response.completed", {"response": response}))
        return frames

    def _fail(self) -> list[bytes]:
        if self.failed:
            return []
        self.failed = True
        self._finished = True
        frames: list[bytes] = []
        started = self.start()
        if started:
            frames.append(started)
        frames.append(
            self._frame(
                "response.failed",
                {
                    "response": {
                        "id": self._response_id,
                        "object": "response",
                        "created_at": self._created_at,
                        "status": "failed",
                        "model": self.response_model,
                        "output": [],
                        "error": {
                            "code": "union_upstream_error",
                            "message": "Union upstream emitted an error",
                        },
                    }
                },
            )
        )
        return frames

    def _handle_event(self, event_type: str, data: dict) -> list[bytes]:
        if event_type == "error" or data.get("type") == "error":
            return self._fail()
        output: list[bytes] = []
        if event_type == "message_start":
            message = data.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                if isinstance(usage, dict):
                    self._usage.update(usage)
            started = self.start()
            if started:
                output.append(started)
        elif event_type == "content_block_start":
            index = data.get("index")
            block = data.get("content_block")
            if isinstance(index, int) and isinstance(block, dict):
                output.extend(self._new_block(index, block.get("type", ""), block))
        elif event_type == "content_block_delta":
            index = data.get("index")
            delta = data.get("delta")
            if not isinstance(index, int) or not isinstance(delta, dict):
                return output
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                output.extend(self._new_block(index, "text"))
                state = self._blocks[index]
                value = delta.get("text")
                if isinstance(value, str) and value:
                    state["text"] += value
                    output.append(
                        self._frame(
                            "response.output_text.delta",
                            {
                                "item_id": state["item"]["id"],
                                "output_index": state["index"],
                                "content_index": 0,
                                "delta": value,
                            },
                        )
                    )
            elif delta_type == "thinking_delta":
                output.extend(self._new_block(index, "thinking"))
                state = self._blocks[index]
                value = delta.get("thinking")
                if isinstance(value, str) and value:
                    state["text"] += value
                    output.append(
                        self._frame(
                            "response.reasoning_summary_text.delta",
                            {
                                "item_id": state["item"]["id"],
                                "output_index": state["index"],
                                "delta": value,
                            },
                        )
                    )
            elif delta_type == "input_json_delta":
                output.extend(self._new_block(index, "tool_use"))
                state = self._blocks[index]
                value = delta.get("partial_json")
                if isinstance(value, str) and value:
                    state["arguments"] += value
                    state["item"]["arguments"] = state["arguments"]
                    output.append(
                        self._frame(
                            "response.function_call_arguments.delta",
                            {
                                "item_id": state["item"]["id"],
                                "output_index": state["index"],
                                "delta": value,
                            },
                        )
                    )
        elif event_type == "content_block_stop":
            index = data.get("index")
            if isinstance(index, int):
                output.extend(self._close_block(index))
        elif event_type == "message_delta":
            delta = data.get("delta")
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str):
                    self._stop_reason = stop_reason
            usage = data.get("usage")
            if isinstance(usage, dict):
                self._usage.update(usage)
        elif event_type == "message_stop":
            started = self.start()
            if started:
                output.append(started)
            output.extend(self._finish())
        return output

    def _consume_frame(self, frame: bytes) -> bytes:
        event_type = ""
        data_lines: list[bytes] = []
        for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
            if line.startswith(b"event:"):
                event_type = line[6:].strip().decode("utf-8", errors="replace")
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return b""
        raw = b"\n".join(data_lines)
        if raw == b"[DONE]":
            return b"".join(self._finish())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return b""
        if not isinstance(data, dict):
            return b""
        if not event_type:
            event_type = data.get("type", "") if isinstance(data.get("type"), str) else ""
        return b"".join(self._handle_event(event_type, data))

    def feed(self, data: bytes, final: bool = False) -> bytes:
        self._buffer += data
        output: list[bytes] = []
        while True:
            boundaries = [
                index
                for index in (
                    self._buffer.find(b"\n\n"),
                    self._buffer.find(b"\r\n\r\n"),
                )
                if index >= 0
            ]
            if not boundaries:
                break
            index = min(boundaries)
            delimiter = (
                b"\r\n\r\n"
                if self._buffer[index : index + 4] == b"\r\n\r\n"
                else b"\n\n"
            )
            frame = self._buffer[:index]
            self._buffer = self._buffer[index + len(delimiter) :]
            output.append(self._consume_frame(frame))
        if final:
            if self._buffer.strip():
                output.append(self._consume_frame(self._buffer))
                self._buffer = b""
            if not self._finished and not self.failed:
                output.extend(self._finish())
        return b"".join(output)

    def response_object(self) -> dict:
        self.feed(b"", final=True)
        status = "incomplete" if self._stop_reason == "max_tokens" else "completed"
        response = {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": status,
            "model": self.response_model,
            "output": self._output,
            "usage": _response_usage(self._usage),
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return response


async def _keepalive(response: aiohttp.web.StreamResponse) -> None:
    try:
        while True:
            await asyncio.sleep(10.0)
            await response.write(b": union-native-keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        raise


async def handle_union_request(
    req: aiohttp.web.Request,
    session: aiohttp.ClientSession,
    route: UnionAnthropicRoute,
    chat_request: dict,
    response_model: str,
    tool_name_map: Mapping[str, tuple[str | None, str]],
    provider_session: str | None,
    caller_stream: bool,
    response_rewriter: Callable[[dict], object] | None = None,
    event_rewriter: Callable[[dict], object] | None = None,
) -> aiohttp.web.StreamResponse | aiohttp.web.Response:
    native_request, tool_id_map = build_anthropic_request(chat_request, route)
    headers = _provider_headers(route, provider_session)
    if headers is None:
        message = f"Union provider credential environment variable is unavailable: {route.api_key_env}"
        log.error(message)
        return aiohttp.web.json_response(
            {"error": {"type": "configuration_error", "message": message}},
            status=502,
        )
    payload = json.dumps(native_request, ensure_ascii=False, separators=(",", ":")).encode()

    stream_response: aiohttp.web.StreamResponse | None = None
    keepalive_task: asyncio.Task | None = None
    if caller_stream:
        stream_response = aiohttp.web.StreamResponse(status=200)
        stream_response.headers["Content-Type"] = "text/event-stream"
        stream_response.headers["Cache-Control"] = "no-cache"
        stream_response.headers["X-Accel-Buffering"] = "no"
        await stream_response.prepare(req)
        await stream_response.write(b": union-native-start\n\n")
        keepalive_task = asyncio.create_task(_keepalive(stream_response))

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            accepted = False
            try:
                async with session.request(
                    "POST",
                    route.api_base,
                    data=payload,
                    headers={**headers, "Content-Length": str(len(payload))},
                    compress=False,
                    timeout=aiohttp.ClientTimeout(total=4200),
                ) as upstream:
                    accepted = True
                    content_type = upstream.headers.get("Content-Type", "")
                    is_sse = "text/event-stream" in content_type.lower()
                    if upstream.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                        await upstream.read()
                        delay = RETRY_BACKOFF_SECONDS[attempt - 1]
                        log.warning(
                            "Union native upstream transient status=%d attempt=%d/%d; retrying",
                            upstream.status,
                            attempt,
                            MAX_ATTEMPTS,
                        )
                        if stream_response is not None:
                            await stream_response.write(
                                f": union-native-retry-{attempt}\n\n".encode()
                            )
                        await asyncio.sleep(delay)
                        continue

                    if not 200 <= upstream.status < 300:
                        error_body = await upstream.read()
                        if stream_response is not None:
                            await stream_response.write(
                                _failure_sse(
                                    response_model,
                                    "Union upstream request failed",
                                )
                            )
                            await stream_response.write_eof()
                            return stream_response
                        return aiohttp.web.Response(
                            status=upstream.status,
                            body=error_body,
                            content_type=content_type.split(";", 1)[0]
                            or "application/json",
                        )

                    if stream_response is not None:
                        if not is_sse:
                            provider_response = json.loads(await upstream.read())
                            if not isinstance(provider_response, dict):
                                raise ValueError("Union provider response is not an object")
                            response_obj = anthropic_response_to_responses(
                                provider_response,
                                response_model,
                                tool_name_map,
                                tool_id_map,
                            )
                            if response_rewriter is not None:
                                response_rewriter(response_obj)
                            await stream_response.write(
                                response_to_sse(response_obj, include_output_events=True)
                            )
                        else:
                            bridge = AnthropicStreamBridge(
                                response_model,
                                tool_name_map,
                                tool_id_map,
                                event_rewriter=event_rewriter,
                            )
                            await stream_response.write(bridge.start())
                            async for chunk in upstream.content.iter_any():
                                if not chunk:
                                    continue
                                converted = bridge.feed(chunk)
                                if converted:
                                    await stream_response.write(converted)
                                if bridge.failed:
                                    break
                            if not bridge.failed:
                                converted = bridge.feed(b"", final=True)
                                if converted:
                                    await stream_response.write(converted)
                        await stream_response.write_eof()
                        return stream_response

                    upstream_body = await upstream.read()
                    if is_sse:
                        bridge = AnthropicStreamBridge(
                            response_model,
                            tool_name_map,
                            tool_id_map,
                            event_rewriter=event_rewriter,
                        )
                        bridge.feed(upstream_body, final=True)
                        if bridge.failed:
                            return aiohttp.web.Response(
                                status=502,
                                text="union-anthropic-bridge: upstream request failed",
                            )
                        response_obj = bridge.response_object()
                    else:
                        provider_response = json.loads(upstream_body)
                        if not isinstance(provider_response, dict):
                            raise ValueError("Union provider response is not an object")
                        response_obj = anthropic_response_to_responses(
                            provider_response,
                            response_model,
                            tool_name_map,
                            tool_id_map,
                        )
                    if response_rewriter is not None:
                        response_rewriter(response_obj)
                    return aiohttp.web.Response(
                        status=200,
                        body=json.dumps(
                            response_obj,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode(),
                        content_type="application/json",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not accepted and attempt < MAX_ATTEMPTS:
                    delay = RETRY_BACKOFF_SECONDS[attempt - 1]
                    log.warning(
                        "Union native upstream connection failure attempt=%d/%d; retrying: %s",
                        attempt,
                        MAX_ATTEMPTS,
                        type(exc).__name__,
                    )
                    if stream_response is not None:
                        await stream_response.write(
                            f": union-native-retry-{attempt}\n\n".encode()
                        )
                    await asyncio.sleep(delay)
                    continue
                log.warning("Union native request failed: %s", type(exc).__name__)
                if stream_response is not None:
                    await stream_response.write(
                        _failure_sse(
                            response_model,
                            "Union upstream request failed",
                        )
                    )
                    await stream_response.write_eof()
                    return stream_response
                return aiohttp.web.Response(
                    status=502,
                    text="union-anthropic-bridge: upstream request failed",
                )
        if stream_response is not None:
            await stream_response.write(
                _failure_sse(response_model, "Union upstream request failed")
            )
            await stream_response.write_eof()
            return stream_response
        return aiohttp.web.Response(
            status=502,
            text="union-anthropic-bridge: upstream request failed",
        )
    finally:
        if keepalive_task is not None:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
                pass
