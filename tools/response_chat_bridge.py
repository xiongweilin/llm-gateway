"""Translate the Responses surface to Chat Completions for chat-mode models.

The public client may have one global base URL.  The unified ingress therefore
keeps ``/v1/responses`` as the client-facing surface and internally sends
chat-mode models through the Chat Completions ingress.  The conversion here is
deliberately limited to standard messages, function tools, tool calls, and
streaming events; Responses-only control-plane paths never enter this module.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable


CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
def _json_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _content_to_chat(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str):
                parts.append({"type": "text", "text": text})
            continue
        if part_type in {"input_image", "output_image", "image_url"}:
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                image_part = {"type": "image_url", "image_url": {"url": image_url}}
                detail = part.get("detail")
                if isinstance(detail, str):
                    image_part["image_url"]["detail"] = detail
                parts.append(image_part)
            continue
        if part_type == "refusal" and isinstance(part.get("refusal"), str):
            parts.append({"type": "text", "text": part["refusal"]})

    return parts


def _tool_name(
    item: dict,
    tool_name_map: dict[str, tuple[str | None, str]],
) -> str:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        name = "tool"
    namespace = item.get("namespace")
    if isinstance(namespace, str) and namespace:
        flat = f"{namespace}__{name}"
        tool_name_map.setdefault(flat, (namespace, name))
        return flat
    return name


def _flatten_tools(
    tools: Iterable,
    tool_name_map: dict[str, tuple[str | None, str]],
) -> list[dict]:
    flattened: list[dict] = []

    def visit(tool, namespace: str | None = None) -> None:
        if not isinstance(tool, dict):
            return
        tool_type = tool.get("type")
        if tool_type == "namespace":
            child_namespace = tool.get("name")
            if not isinstance(child_namespace, str) or not child_namespace:
                child_namespace = namespace
            for child in tool.get("tools", []) if isinstance(tool.get("tools"), list) else []:
                visit(child, child_namespace)
            return
        if tool_type != "function":
            return

        name = tool.get("name")
        if not isinstance(name, str) or not name:
            return
        flat_name = name
        if namespace:
            flat_name = f"{namespace}__{name}"
            tool_name_map[flat_name] = (namespace, name)

        parameters = tool.get("parameters")
        if not isinstance(parameters, dict):
            parameters = tool.get("input_schema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}

        function = {"name": flat_name, "parameters": parameters}
        description = tool.get("description")
        if isinstance(description, str) and description:
            function["description"] = description
        flattened.append({"type": "function", "function": function})

    for tool in tools:
        visit(tool)
    return flattened


def _append_function_call(
    messages: list[dict],
    item: dict,
    tool_name_map: dict[str, tuple[str | None, str]],
) -> None:
    name = _tool_name(item, tool_name_map)
    call_id = item.get("call_id") or item.get("id") or f"call_{len(messages)}"
    arguments = _json_text(item.get("arguments", item.get("input", "")))
    if (
        messages
        and messages[-1].get("role") == "assistant"
        and isinstance(messages[-1].get("tool_calls"), list)
        and not messages[-1].get("content")
    ):
        assistant = messages[-1]
    else:
        assistant = {"role": "assistant", "content": None, "tool_calls": []}
        messages.append(assistant)
    assistant["tool_calls"].append(
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
    )


def _append_tool_output(messages: list[dict], item: dict) -> None:
    call_id = item.get("call_id") or item.get("id") or "unknown-call"
    output = item.get("output", item.get("content", ""))
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _json_text(output),
        }
    )


def _append_message(messages: list[dict], item: dict) -> None:
    role = item.get("role")
    if role == "developer":
        role = "system"
    if role not in {"system", "user", "assistant", "tool"}:
        role = "user"
    messages.append({"role": role, "content": _content_to_chat(item.get("content"))})


def responses_to_chat_request(
    body: dict,
) -> tuple[dict, dict[str, tuple[str | None, str]]]:
    """Convert one Responses request to a Chat Completions request.

    The second return value maps flattened names back to their original
    namespace/name pair for the Responses response conversion.
    """
    chat: dict = {"model": body.get("model")}
    messages: list[dict] = []
    tool_name_map: dict[str, tuple[str | None, str]] = {}

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    input_value = body.get("input")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"message", None} and (
                item_type == "message" or "role" in item
            ):
                _append_message(messages, item)
            elif item_type in {"function_call", "custom_tool_call", "local_shell_call"}:
                _append_function_call(messages, item, tool_name_map)
            elif item_type in {
                "function_call_output",
                "custom_tool_call_output",
                "local_shell_call_output",
            }:
                _append_tool_output(messages, item)

    if not messages:
        messages.append({"role": "user", "content": ""})
    chat["messages"] = messages

    tools = body.get("tools")
    if isinstance(tools, list):
        flattened_tools = _flatten_tools(tools, tool_name_map)
        if flattened_tools:
            chat["tools"] = flattened_tools
            # Console Go currently accepts only the generic choice.
            chat["tool_choice"] = "auto"

    if isinstance(body.get("stream"), bool):
        chat["stream"] = body["stream"]
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_completion_tokens" in body:
        chat["max_tokens"] = body["max_completion_tokens"]

    for key in (
        "temperature",
        "top_p",
        "stop",
        "seed",
        "user",
        "response_format",
        "metadata",
        "stream_options",
        "service_tier",
        "parallel_tool_calls",
        "extra_headers",
    ):
        if key in body:
            chat[key] = body[key]

    return chat, tool_name_map


def _chat_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        return "".join(texts)
    return ""


def _response_usage(usage) -> dict:
    source_usage = usage if isinstance(usage, dict) else {}
    normalized = dict(source_usage)

    def token_count(*keys: str) -> int | None:
        for key in keys:
            value = normalized.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    # Responses clients require the canonical usage fields even when the
    # Chat Completions provider omits usage on a streamed response.  Preserve
    # provider values when present, and use zero as an explicit "unavailable"
    # value instead of emitting an invalid empty usage object.
    input_tokens = token_count("input_tokens", "prompt_tokens")
    output_tokens = token_count("output_tokens", "completion_tokens")
    total_tokens = token_count("total_tokens")
    input_tokens = 0 if input_tokens is None else input_tokens
    output_tokens = 0 if output_tokens is None else output_tokens
    total_tokens = (
        input_tokens + output_tokens
        if total_tokens is None
        else total_tokens
    )
    normalized["input_tokens"] = input_tokens
    normalized["output_tokens"] = output_tokens
    normalized["total_tokens"] = total_tokens
    prompt_details = source_usage.get("prompt_tokens_details")
    if "input_tokens_details" not in normalized and isinstance(prompt_details, dict):
        normalized["input_tokens_details"] = prompt_details
    completion_details = source_usage.get("completion_tokens_details")
    if "output_tokens_details" not in normalized and isinstance(completion_details, dict):
        normalized["output_tokens_details"] = completion_details
    normalized.setdefault("input_tokens_details", {"cached_tokens": 0})
    normalized.setdefault("output_tokens_details", {"reasoning_tokens": 0})
    return normalized


def _restore_tool_name(
    name: str,
    tool_name_map: dict[str, tuple[str | None, str]],
) -> tuple[str, str | None]:
    mapped = tool_name_map.get(name)
    if mapped is None:
        return name, None
    namespace, original = mapped
    return original, namespace


def chat_response_to_responses(
    chat_response: dict,
    response_model: str | None = None,
    tool_name_map: dict[str, tuple[str | None, str]] | None = None,
) -> dict:
    """Convert a non-streaming Chat Completions response to Responses."""
    tool_name_map = tool_name_map or {}
    chat_id = chat_response.get("id")
    response_id = chat_id if isinstance(chat_id, str) and chat_id.startswith("resp_") else f"resp_{chat_id or int(time.time() * 1000)}"
    output: list[dict] = []
    output_text: list[str] = []
    choices = chat_response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}

    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        output.append(
            {
                "id": f"{response_id}_reasoning",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )

    text = _chat_text(message.get("content"))
    if text:
        output_text.append(text)
        output.append(
            {
                "id": f"{response_id}_message",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict):
                function = {}
            name = function.get("name") if isinstance(function.get("name"), str) else "tool"
            name, namespace = _restore_tool_name(name, tool_name_map)
            item = {
                "id": call.get("id") or f"{response_id}_function_{index}",
                "type": "function_call",
                "call_id": call.get("id") or f"{response_id}_call_{index}",
                "name": name,
                "arguments": _json_text(function.get("arguments", "")),
            }
            if namespace:
                item["namespace"] = namespace
            output.append(item)

    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    status = "incomplete" if finish_reason == "length" else "completed"
    response = {
        "id": response_id,
        "object": "response",
        "created_at": chat_response.get("created", int(time.time())),
        "status": status,
        "model": response_model or chat_response.get("model"),
        "output": output,
        "usage": _response_usage(chat_response.get("usage")),
    }
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    if output_text:
        response["output_text"] = "".join(output_text)
    return response


def _sse_frame(event_type: str, payload: dict) -> bytes:
    event = dict(payload)
    event.setdefault("type", event_type)
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {encoded}\n\n".encode()


class ChatStreamBridge:
    """Incrementally convert Chat Completions SSE chunks to Responses SSE."""

    def __init__(
        self,
        response_model: str | None = None,
        tool_name_map=None,
        restore_tool_names: bool = False,
    ):
        self.response_model = response_model
        self.tool_name_map = tool_name_map or {}
        self.restore_tool_names = restore_tool_names
        self._buffer = b""
        self._response_id: str | None = None
        self._created_at = int(time.time())
        self._started = False
        self._finished = False
        self._output: list[dict] = []
        self._message: dict | None = None
        self._reasoning: dict | None = None
        self._tools: dict[int, dict] = {}
        self._usage: dict = {}
        self._finish_reason: str | None = None

    def _start(self, chunk: dict) -> list[bytes]:
        if self._started:
            return []
        chat_id = chunk.get("id")
        self._response_id = (
            chat_id
            if isinstance(chat_id, str) and chat_id.startswith("resp_")
            else f"resp_{chat_id or int(time.time() * 1000)}"
        )
        if isinstance(chunk.get("created"), int):
            self._created_at = chunk["created"]
        self._started = True
        response = {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": "in_progress",
            "model": self.response_model or chunk.get("model"),
            "output": [],
        }
        return [
            _sse_frame("response.created", {"response": response}),
            _sse_frame("response.in_progress", {"response": response}),
        ]

    def _start_message(self) -> list[bytes]:
        if self._message is not None:
            return []
        response_id = self._response_id or "resp_stream"
        item = {
            "id": f"{response_id}_message",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        self._message = {"item": item, "text": "", "index": len(self._output)}
        self._output.append(item)
        return [
            _sse_frame(
                "response.output_item.added",
                {"output_index": self._message["index"], "item": item},
            ),
            _sse_frame(
                "response.content_part.added",
                {
                    "output_index": self._message["index"],
                    "content_index": 0,
                    "item_id": item["id"],
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def _start_reasoning(self) -> list[bytes]:
        if self._reasoning is not None:
            return []
        response_id = self._response_id or "resp_stream"
        item = {
            "id": f"{response_id}_reasoning",
            "type": "reasoning",
            "summary": [],
        }
        self._reasoning = {"item": item, "text": "", "index": len(self._output)}
        self._output.append(item)
        return [
            _sse_frame(
                "response.output_item.added",
                {"output_index": self._reasoning["index"], "item": item},
            )
        ]

    def _start_tool(self, index: int, call: dict) -> list[bytes]:
        state = self._tools.get(index)
        if state is not None:
            return []
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") if isinstance(function.get("name"), str) else "tool"
        namespace = None
        if self.restore_tool_names:
            mapped = self.tool_name_map.get(name)
            if mapped is not None:
                namespace, name = mapped
        call_id = call.get("id") or f"{self._response_id}_call_{index}"
        item = {
            "id": call_id,
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": "",
        }
        if namespace:
            item["namespace"] = namespace
        state = {"item": item, "index": len(self._output), "arguments": ""}
        self._tools[index] = state
        self._output.append(item)
        return [
            _sse_frame(
                "response.output_item.added",
                {"output_index": state["index"], "item": item},
            )
        ]

    def _handle_chunk(self, chunk: dict) -> list[bytes]:
        output = self._start(chunk)
        if isinstance(chunk.get("usage"), dict):
            self._usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return output
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                output.extend(self._start_reasoning())
                self._reasoning["text"] += reasoning
                output.append(
                    _sse_frame(
                        "response.reasoning_summary_text.delta",
                        {
                            "item_id": self._reasoning["item"]["id"],
                            "output_index": self._reasoning["index"],
                            "delta": reasoning,
                        },
                    )
                )
            content = delta.get("content")
            if isinstance(content, str) and content:
                output.extend(self._start_message())
                self._message["text"] += content
                output.append(
                    _sse_frame(
                        "response.output_text.delta",
                        {
                            "item_id": self._message["item"]["id"],
                            "output_index": self._message["index"],
                            "content_index": 0,
                            "delta": content,
                        },
                    )
                )
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index")
                    if not isinstance(index, int):
                        index = len(self._tools)
                    output.extend(self._start_tool(index, call))
                    state = self._tools[index]
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    if isinstance(function.get("name"), str) and function["name"]:
                        state["item"]["name"] = function["name"]
                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        state["arguments"] += arguments
                        state["item"]["arguments"] = state["arguments"]
                        output.append(
                            _sse_frame(
                                "response.function_call_arguments.delta",
                                {
                                    "item_id": state["item"]["id"],
                                    "output_index": state["index"],
                                    "delta": arguments,
                                },
                            )
                        )
            if choice.get("finish_reason") is not None:
                self._finish_reason = choice["finish_reason"]
        return output

    def _finish(self) -> list[bytes]:
        if self._finished:
            return []
        self._finished = True
        output: list[bytes] = []
        if self._reasoning is not None:
            item = self._reasoning["item"]
            item["summary"] = [{"type": "summary_text", "text": self._reasoning["text"]}]
            output.append(
                _sse_frame(
                    "response.reasoning_summary_text.done",
                    {
                        "item_id": item["id"],
                        "output_index": self._reasoning["index"],
                        "text": self._reasoning["text"],
                    },
                )
            )
            output.append(
                _sse_frame(
                    "response.output_item.done",
                    {"output_index": self._reasoning["index"], "item": item},
                )
            )
        if self._message is not None:
            item = self._message["item"]
            text = self._message["text"]
            item["status"] = "completed"
            item["content"] = [{"type": "output_text", "text": text, "annotations": []}]
            output.extend(
                [
                    _sse_frame(
                        "response.output_text.done",
                        {
                            "item_id": item["id"],
                            "output_index": self._message["index"],
                            "content_index": 0,
                            "text": text,
                        },
                    ),
                    _sse_frame(
                        "response.content_part.done",
                        {
                            "item_id": item["id"],
                            "output_index": self._message["index"],
                            "content_index": 0,
                            "part": item["content"][0],
                        },
                    ),
                    _sse_frame(
                        "response.output_item.done",
                        {"output_index": self._message["index"], "item": item},
                    ),
                ]
            )
        for state in self._tools.values():
            item = state["item"]
            name = item.get("name")
            mapped = self.tool_name_map.get(name)
            if mapped is not None:
                namespace, original = mapped
                if namespace:
                    item["namespace"] = namespace
                else:
                    item.pop("namespace", None)
                item["name"] = original
            item["arguments"] = state["arguments"]
            output.extend(
                [
                    _sse_frame(
                        "response.function_call_arguments.done",
                        {
                            "item_id": item["id"],
                            "output_index": state["index"],
                            "arguments": item["arguments"],
                        },
                    ),
                    _sse_frame(
                        "response.output_item.done",
                        {"output_index": state["index"], "item": item},
                    ),
                ]
            )
        response = {
            "id": self._response_id or f"resp_{int(time.time() * 1000)}",
            "object": "response",
            "created_at": self._created_at,
            "status": "incomplete" if self._finish_reason == "length" else "completed",
            "model": self.response_model,
            "output": self._output,
            "usage": _response_usage(self._usage),
        }
        if response["status"] == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        output.append(_sse_frame("response.completed", {"response": response}))
        return output

    def _consume_frame(self, frame: bytes) -> bytes:
        data_lines = []
        for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return b""
        data = b"\n".join(data_lines)
        if data == b"[DONE]":
            return b"".join(self._finish())
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return b""
        if not isinstance(chunk, dict):
            return b""
        return b"".join(self._handle_chunk(chunk))

    def feed(self, data: bytes, final: bool = False) -> bytes:
        self._buffer += data
        output: list[bytes] = []
        while True:
            boundaries = [
                index for index in (
                    self._buffer.find(b"\n\n"),
                    self._buffer.find(b"\r\n\r\n"),
                ) if index >= 0
            ]
            if not boundaries:
                break
            index = min(boundaries)
            delimiter = b"\r\n\r\n" if self._buffer[index:index + 4] == b"\r\n\r\n" else b"\n\n"
            frame = self._buffer[:index]
            self._buffer = self._buffer[index + len(delimiter):]
            output.append(self._consume_frame(frame))
        if final:
            if self._buffer.strip():
                output.append(self._consume_frame(self._buffer))
                self._buffer = b""
            if not self._finished:
                output.extend(self._finish())
        return b"".join(output)

    def response_object(self) -> dict:
        self.feed(b"", final=True)
        return {
            "id": self._response_id or f"resp_{int(time.time() * 1000)}",
            "object": "response",
            "created_at": self._created_at,
            "status": "incomplete" if self._finish_reason == "length" else "completed",
            "model": self.response_model,
            "output": self._output,
            "usage": _response_usage(self._usage),
        }


def chat_sse_to_responses_json(
    body: bytes,
    response_model: str | None = None,
    tool_name_map=None,
) -> bytes:
    bridge = ChatStreamBridge(response_model, tool_name_map)
    bridge.feed(body, final=True)
    return json.dumps(bridge.response_object(), ensure_ascii=False, separators=(",", ":")).encode()


def response_to_sse(
    response: dict,
    include_output_events: bool = False,
) -> bytes:
    """Emit a Responses stream for a converted JSON response.

    The default keeps the historical lifecycle-only wrapper for existing chat
    routes.  Some providers accept a buffered request but cannot reliably
    stream; those callers can request the complete output-item event sequence
    so a Responses client still receives text and tool calls through its
    streaming interface.
    """
    created = dict(response)
    created["status"] = "in_progress"
    if include_output_events:
        created["output"] = []
    frames = [
        _sse_frame("response.created", {"response": created}),
        _sse_frame("response.in_progress", {"response": created}),
    ]

    if include_output_events:
        response_id = response.get("id") or "resp_stream"
        output = response.get("output")
        if isinstance(output, list):
            for output_index, item in enumerate(output):
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or f"{response_id}_item_{output_index}"
                item_type = item.get("type")
                added_item = dict(item)
                if item_type == "message":
                    added_item["status"] = "in_progress"
                    added_item["content"] = []
                elif item_type == "reasoning":
                    added_item["summary"] = []
                elif item_type == "function_call":
                    added_item["arguments"] = ""
                elif item_type == "custom_tool_call":
                    added_item["input"] = ""
                frames.append(
                    _sse_frame(
                        "response.output_item.added",
                        {"output_index": output_index, "item": added_item},
                    )
                )

                if item_type == "message":
                    content = item.get("content")
                    if isinstance(content, str):
                        content = [{"type": "output_text", "text": content, "annotations": []}]
                    if not isinstance(content, list):
                        content = []
                    for content_index, part in enumerate(content):
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text")
                        if not isinstance(text, str):
                            text = ""
                        part_type = part.get("type") or "output_text"
                        added_part = dict(part)
                        added_part["type"] = part_type
                        added_part["text"] = ""
                        added_part.setdefault("annotations", [])
                        frames.append(
                            _sse_frame(
                                "response.content_part.added",
                                {
                                    "output_index": output_index,
                                    "content_index": content_index,
                                    "item_id": item_id,
                                    "part": added_part,
                                },
                            )
                        )
                        if part_type == "output_text" and text:
                            frames.append(
                                _sse_frame(
                                    "response.output_text.delta",
                                    {
                                        "item_id": item_id,
                                        "output_index": output_index,
                                        "content_index": content_index,
                                        "delta": text,
                                    },
                                )
                            )
                        if part_type == "output_text":
                            frames.append(
                                _sse_frame(
                                    "response.output_text.done",
                                    {
                                        "item_id": item_id,
                                        "output_index": output_index,
                                        "content_index": content_index,
                                        "text": text,
                                    },
                                )
                            )
                        frames.append(
                            _sse_frame(
                                "response.content_part.done",
                                {
                                    "item_id": item_id,
                                    "output_index": output_index,
                                    "content_index": content_index,
                                    "part": part,
                                },
                            )
                        )
                elif item_type == "reasoning":
                    summary = item.get("summary")
                    if isinstance(summary, list):
                        for part in summary:
                            if not isinstance(part, dict) or part.get("type") != "summary_text":
                                continue
                            text = part.get("text")
                            if not isinstance(text, str):
                                text = ""
                            if text:
                                frames.append(
                                    _sse_frame(
                                        "response.reasoning_summary_text.delta",
                                        {
                                            "item_id": item_id,
                                            "output_index": output_index,
                                            "delta": text,
                                        },
                                    )
                                )
                            frames.append(
                                _sse_frame(
                                    "response.reasoning_summary_text.done",
                                    {
                                        "item_id": item_id,
                                        "output_index": output_index,
                                        "text": text,
                                    },
                                )
                            )
                elif item_type == "function_call":
                    arguments = item.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = _json_text(arguments)
                    if arguments:
                        frames.append(
                            _sse_frame(
                                "response.function_call_arguments.delta",
                                {
                                    "item_id": item_id,
                                    "output_index": output_index,
                                    "delta": arguments,
                                },
                            )
                        )
                    frames.append(
                        _sse_frame(
                            "response.function_call_arguments.done",
                            {
                                "item_id": item_id,
                                "output_index": output_index,
                                "arguments": arguments,
                            },
                        )
                    )
                elif item_type == "custom_tool_call":
                    input_value = item.get("input")
                    if not isinstance(input_value, str):
                        input_value = _json_text(input_value)
                    if input_value:
                        frames.append(
                            _sse_frame(
                                "response.custom_tool_call_input.delta",
                                {
                                    "item_id": item_id,
                                    "output_index": output_index,
                                    "delta": input_value,
                                },
                            )
                        )
                    frames.append(
                        _sse_frame(
                            "response.custom_tool_call_input.done",
                            {
                                "item_id": item_id,
                                "output_index": output_index,
                                "input": input_value,
                            },
                        )
                    )

                frames.append(
                    _sse_frame(
                        "response.output_item.done",
                        {"output_index": output_index, "item": dict(item)},
                    )
                )

    frames.append(_sse_frame("response.completed", {"response": response}))
    return b"".join(frames)
