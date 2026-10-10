"""Translate Codex Responses calls into Anthropic Messages traffic."""

from __future__ import annotations

import asyncio
import codecs
import copy
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import aiohttp
from aiohttp import web


log = logging.getLogger("agent-gateway")
MAX_TOKENS_DEFAULT = 4096


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _error(message: str, error_type: str = "invalid_request_error", status: int = 400):
    return web.json_response({"error": {"type": error_type, "message": message}}, status=status)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False) if content is not None else ""
    pieces: list[str] = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            pieces.append(part["text"])
        elif isinstance(part, str):
            pieces.append(part)
    return "".join(pieces)


def _image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    value = part.get("image_url", part.get("url"))
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("data:") and ";base64," in value:
        metadata, data = value.split(",", 1)
        media_type = metadata[5:].split(";", 1)[0] or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": value}}


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": _text_from_content(content)}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
        elif part_type in {"input_image", "image"}:
            image = _image_block(part)
            if image is not None:
                blocks.append(image)
    return blocks


def _append_message(messages: list[dict[str, Any]], role: str, content: list[dict[str, Any]]) -> None:
    if not content:
        content = [{"type": "text", "text": ""}]
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
    else:
        messages.append({"role": role, "content": content})


def _tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def strict_schema(schema: Any) -> Any:
        if isinstance(schema, list):
            return [strict_schema(item) for item in schema]
        if not isinstance(schema, dict):
            return schema
        updated = copy.deepcopy(schema)
        properties = updated.get("properties")
        if updated.get("type") == "object" and not isinstance(properties, dict):
            properties = {}
        if isinstance(properties, dict):
            updated["properties"] = {
                name: strict_schema(value) for name, value in properties.items()
            }
            updated["additionalProperties"] = False
        for key in ("items", "allOf", "anyOf", "oneOf"):
            if key in updated:
                updated[key] = strict_schema(updated[key])
        return updated

    for tool in payload.get("tools", []):
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function":
            name = tool.get("name")
            schema = tool.get("parameters", tool.get("input_schema"))
            if isinstance(name, str) and name and isinstance(schema, dict):
                converted = {
                    "name": name,
                    "description": tool.get("description", ""),
                    "input_schema": strict_schema(schema) if tool.get("strict") is True else schema,
                }
                if tool.get("strict") is True:
                    converted["strict"] = True
                result.append(converted)
        elif tool_type in {"custom", "custom_tool"}:
            name = tool.get("name")
            if isinstance(name, str) and name:
                result.append(
                    {
                        "name": name,
                        "description": tool.get("description") or f"{name} tool",
                        "input_schema": {
                            "type": "object",
                            "properties": {"input": {"type": "string"}},
                            "required": ["input"],
                        },
                    }
                )
    return result


def responses_request_to_messages(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("model is required")
    max_tokens = payload.get("max_output_tokens", MAX_TOKENS_DEFAULT)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise ValueError("max_output_tokens must be a positive integer")

    system_parts: list[str] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append(instructions)
    messages: list[dict[str, Any]] = []
    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        _append_message(messages, "user", [{"type": "text", "text": input_value}])
    elif isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "message")
            if item_type == "message":
                role = item.get("role", "user")
                content = _content_blocks(item.get("content"))
                if role in {"system", "developer"}:
                    system_parts.append(_text_from_content(item.get("content")))
                elif role in {"user", "assistant"}:
                    _append_message(messages, role, content)
            elif item_type == "function_call":
                name = item.get("name")
                call_id = item.get("call_id", item.get("id"))
                if isinstance(name, str) and name and isinstance(call_id, str) and call_id:
                    arguments = item.get("arguments", "{}")
                    try:
                        parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except json.JSONDecodeError:
                        parsed_arguments = {"input": arguments}
                    _append_message(
                        messages,
                        "assistant",
                        [{"type": "tool_use", "id": call_id, "name": name, "input": parsed_arguments}],
                    )
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id:
                    output = _text_from_content(item.get("output"))
                    _append_message(
                        messages,
                        "user",
                        [{"type": "tool_result", "tool_use_id": call_id, "content": output}],
                    )
    else:
        raise ValueError("input must be a string or array")

    if not messages:
        raise ValueError("input must contain at least one user or assistant message")

    result: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
    }
    provider_tools = _tools(payload)
    choice = payload.get("tool_choice")
    forced_tool_instruction: str | None = None
    if choice == "none":
        provider_tools = []
    elif choice == "auto" or (isinstance(choice, dict) and choice.get("type") == "auto"):
        result["tool_choice"] = {"type": "auto"}
    elif isinstance(choice, str) and choice in {"required", "any"}:
        result["tool_choice"] = {"type": "auto"}
        forced_tool_instruction = "Use one of the available tools before answering the user."
    elif isinstance(choice, dict) and choice.get("type") == "function":
        function = choice.get("name")
        if not isinstance(function, str):
            function = (choice.get("function") or {}).get("name") if isinstance(choice.get("function"), dict) else None
        if isinstance(function, str) and function:
            result["tool_choice"] = {"type": "auto"}
            forced_tool_instruction = f"Use the {function} tool before answering the user."
    if forced_tool_instruction:
        system_parts.append(forced_tool_instruction)
    if system_parts:
        result["system"] = "\n\n".join(part for part in system_parts if part)
    if provider_tools:
        result["tools"] = provider_tools
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            result["thinking"] = {"type": "adaptive"}
            result["output_config"] = {"effort": effort}
        elif effort in {"minimal", "none"}:
            result["thinking"] = {"type": "between_tools"}
            result["output_config"] = {"effort": "low"}
        elif effort is not None:
            raise ValueError(f"unsupported Codex reasoning effort for Sonnet 5.5: {effort}")
    for key in ("temperature", "top_p"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value != 1:
                raise ValueError(f"Claude Sonnet 5.5 only accepts the default {key}")
    return result


def _sse_frame(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


async def _parse_sse(content: aiohttp.StreamReader) -> AsyncIterator[tuple[str, Any]]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""

    def pop_frame() -> tuple[str, Any] | None:
        nonlocal buffer
        match = re.search(r"\r?\n\r?\n", buffer)
        if match is None:
            return None
        frame = buffer[:match.start()]
        buffer = buffer[match.end():]
        event_name = "message"
        data_lines: list[str] = []
        for line in frame.replace("\r\n", "\n").split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return (event_name, None)
        data_text = "\n".join(data_lines)
        if data_text == "[DONE]":
            return (event_name, data_text)
        try:
            return event_name, json.loads(data_text)
        except json.JSONDecodeError:
            return event_name, data_text

    async for chunk in content.iter_any():
        if not chunk:
            continue
        buffer += decoder.decode(chunk)
        while True:
            item = pop_frame()
            if item is None:
                break
            yield item
    buffer += decoder.decode(b"", final=True)
    while True:
        item = pop_frame()
        if item is None:
            break
        yield item
    if buffer.strip():
        frame = buffer
        event_name = "message"
        data_lines: list[str] = []
        for line in frame.replace("\r\n", "\n").split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            data_text = "\n".join(data_lines)
            try:
                yield event_name, json.loads(data_text)
            except json.JSONDecodeError:
                yield event_name, data_text


class ResponsesEventAdapter:
    def __init__(self, model: str) -> None:
        self.response_id = f"resp_{uuid4().hex}"
        self.created_at = int(time.time())
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self.output: list[dict[str, Any]] = []
        self.blocks: dict[int, dict[str, Any]] = {}
        self.response: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": "in_progress",
            "model": model,
            "output": self.output,
            "parallel_tool_calls": True,
        }
        self.final_response: dict[str, Any] | None = None

    def initial_events(self) -> list[dict[str, Any]]:
        return [
            {"type": "response.created", "response": copy.deepcopy(self.response)},
            {"type": "response.in_progress", "response": copy.deepcopy(self.response)},
        ]

    def feed(self, event_name: str, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            return []
        if event_name == "message_start":
            message = value.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self.input_tokens = int(usage.get("input_tokens", 0) or 0)
            return []
        if event_name == "content_block_start":
            index = value.get("index")
            block = value.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                return []
            block_type = block.get("type")
            if block_type == "text":
                item = {
                    "id": f"msg_{uuid4().hex}",
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "", "annotations": []}],
                }
                state = {"type": "text", "item": item, "output_index": len(self.output), "text": ""}
                self.output.append(item)
                self.blocks[index] = state
                return [
                    {"type": "response.output_item.added", "response_id": self.response_id, "output_index": state["output_index"], "item": copy.deepcopy(item)},
                    {"type": "response.content_part.added", "response_id": self.response_id, "item_id": item["id"], "output_index": state["output_index"], "content_index": 0, "part": copy.deepcopy(item["content"][0])},
                ]
            if block_type == "tool_use":
                call_id = block.get("id") if isinstance(block.get("id"), str) else f"call_{uuid4().hex}"
                item = {
                    "id": f"fc_{uuid4().hex}",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": block.get("name", "tool"),
                    "arguments": "",
                }
                initial_input = block.get("input")
                arguments = json.dumps(initial_input, ensure_ascii=False, separators=(",", ":")) if isinstance(initial_input, dict) and initial_input else ""
                item["arguments"] = arguments
                state = {"type": "tool", "item": item, "output_index": len(self.output), "arguments": arguments}
                self.output.append(item)
                self.blocks[index] = state
                events = [
                    {"type": "response.output_item.added", "response_id": self.response_id, "output_index": state["output_index"], "item": copy.deepcopy(item)},
                ]
                if arguments:
                    events.append({"type": "response.function_call_arguments.delta", "response_id": self.response_id, "item_id": item["id"], "output_index": state["output_index"], "delta": arguments})
                return events
            return []
        if event_name == "content_block_delta":
            index = value.get("index")
            delta = value.get("delta")
            state = self.blocks.get(index) if isinstance(index, int) else None
            if not isinstance(state, dict) or not isinstance(delta, dict):
                return []
            if state["type"] == "text" and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    state["text"] += text
                    state["item"]["content"][0]["text"] = state["text"]
                    return [{"type": "response.output_text.delta", "response_id": self.response_id, "item_id": state["item"]["id"], "output_index": state["output_index"], "content_index": 0, "delta": text}]
            if state["type"] == "tool" and delta.get("type") == "input_json_delta":
                text = delta.get("partial_json")
                if isinstance(text, str):
                    state["arguments"] += text
                    state["item"]["arguments"] = state["arguments"]
                    return [{"type": "response.function_call_arguments.delta", "response_id": self.response_id, "item_id": state["item"]["id"], "output_index": state["output_index"], "delta": text}]
            return []
        if event_name == "content_block_stop":
            index = value.get("index")
            state = self.blocks.pop(index, None) if isinstance(index, int) else None
            if not isinstance(state, dict):
                return []
            state["item"]["status"] = "completed"
            if state["type"] == "text":
                part = copy.deepcopy(state["item"]["content"][0])
                return [
                    {"type": "response.output_text.done", "response_id": self.response_id, "item_id": state["item"]["id"], "output_index": state["output_index"], "content_index": 0, "text": state["text"]},
                    {"type": "response.content_part.done", "response_id": self.response_id, "item_id": state["item"]["id"], "output_index": state["output_index"], "content_index": 0, "part": part},
                    {"type": "response.output_item.done", "response_id": self.response_id, "output_index": state["output_index"], "item": copy.deepcopy(state["item"])},
                ]
            arguments = state["arguments"] or "{}"
            try:
                parsed = json.loads(arguments)
                state["item"]["arguments"] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            except json.JSONDecodeError:
                state["item"]["arguments"] = arguments
            return [
                {"type": "response.function_call_arguments.done", "response_id": self.response_id, "item_id": state["item"]["id"], "output_index": state["output_index"], "arguments": state["item"]["arguments"]},
                {"type": "response.output_item.done", "response_id": self.response_id, "output_index": state["output_index"], "item": copy.deepcopy(state["item"])},
            ]
        if event_name == "message_delta":
            delta = value.get("delta")
            usage = value.get("usage")
            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                self.stop_reason = delta["stop_reason"]
            if isinstance(usage, dict):
                self.output_tokens = int(usage.get("output_tokens", 0) or 0)
            return []
        if event_name == "message_stop":
            events: list[dict[str, Any]] = []
            for index in list(self.blocks):
                events.extend(self.feed("content_block_stop", {"index": index}))
            incomplete = self.stop_reason == "max_tokens"
            self.response.update(
                {
                    "status": "incomplete" if incomplete else "completed",
                    "completed_at": int(time.time()),
                    "output": self.output,
                    "usage": {
                        "input_tokens": self.input_tokens,
                        "output_tokens": self.output_tokens,
                        "total_tokens": self.input_tokens + self.output_tokens,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                }
            )
            if incomplete:
                self.response["incomplete_details"] = {"reason": "max_output_tokens"}
            self.final_response = copy.deepcopy(self.response)
            event_type = "response.incomplete" if incomplete else "response.completed"
            events.append({"type": event_type, "response": copy.deepcopy(self.final_response)})
            return events
        if event_name == "error":
            error = value.get("error") if isinstance(value.get("error"), dict) else value
            return [{"type": "error", "error": {"type": error.get("type", "upstream_error"), "message": error.get("message", "Anthropic stream failed")}}]
        return []


def _events_from_message(message: dict[str, Any], model: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    adapter = ResponsesEventAdapter(model)
    events = adapter.initial_events()
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    events.extend(adapter.feed("message_start", {"message": {"usage": usage}}))
    for index, block in enumerate(message.get("content", [])):
        if not isinstance(block, dict):
            continue
        start_block = dict(block)
        if start_block.get("type") == "tool_use":
            start_block["input"] = {}
        events.extend(adapter.feed("content_block_start", {"index": index, "content_block": start_block}))
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            events.extend(adapter.feed("content_block_delta", {"index": index, "delta": {"type": "text_delta", "text": block["text"]}}))
        elif block.get("type") == "tool_use":
            raw = block.get("input")
            if isinstance(raw, dict) and raw:
                partial = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
                events.extend(adapter.feed("content_block_delta", {"index": index, "delta": {"type": "input_json_delta", "partial_json": partial}}))
        events.extend(adapter.feed("content_block_stop", {"index": index}))
    stop_reason = message.get("stop_reason")
    events.extend(adapter.feed("message_delta", {"delta": {"stop_reason": stop_reason}, "usage": usage}))
    events.extend(adapter.feed("message_stop", {}))
    return events, adapter.final_response


def _anthropic_error(body: bytes, status: int) -> web.Response:
    message = "Anthropic Messages request failed"
    error_type = "upstream_error"
    try:
        value = json.loads(body)
        error = value.get("error") if isinstance(value, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message", message))
            error_type = str(error.get("type", error_type))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return web.json_response({"error": {"type": error_type, "message": message}}, status=status)


async def handle_responses_as_messages(
    request: web.Request,
    body: bytes,
    messages_url: str,
    session: aiohttp.ClientSession,
):
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        messages_payload = responses_request_to_messages(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return _error(str(exc))

    try:
        async with session.post(
            f"{messages_url.rstrip('/')}/v1/messages",
            data=_json_bytes(messages_payload),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            compress=False,
            timeout=aiohttp.ClientTimeout(total=4200),
        ) as upstream:
            if not 200 <= upstream.status < 300:
                return _anthropic_error(await upstream.read(), upstream.status)
            adapter = ResponsesEventAdapter(str(payload["model"]))
            if "text/event-stream" in upstream.headers.get("Content-Type", "").lower():
                if bool(payload.get("stream", False)):
                    response = web.StreamResponse(
                        status=200,
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                        reason="OK",
                    )
                    response.content_type = "text/event-stream"
                    await response.prepare(request)
                    for event in adapter.initial_events():
                        await response.write(_sse_frame(event["type"], event))
                    async for event_name, value in _parse_sse(upstream.content):
                        for event in adapter.feed(event_name, value):
                            await response.write(_sse_frame(event["type"], event))
                    if adapter.final_response is None:
                        await response.write(_sse_frame("error", {"type": "error", "error": {"type": "upstream_error", "message": "Anthropic stream ended before message_stop"}}))
                    await response.write_eof()
                    return response

                async for event_name, value in _parse_sse(upstream.content):
                    adapter.feed(event_name, value)
                if adapter.final_response is None:
                    return _error("Anthropic stream ended before message_stop", "upstream_error", 502)
                return web.json_response(adapter.final_response)

            message = await upstream.json()
            events, final_response = _events_from_message(message, str(payload["model"]))
            if final_response is None:
                return _error("Anthropic response did not contain a final message", "upstream_error", 502)
            if bool(payload.get("stream", False)):
                response = web.StreamResponse(
                    status=200,
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    reason="OK",
                )
                response.content_type = "text/event-stream"
                await response.prepare(request)
                for event in events:
                    await response.write(_sse_frame(event["type"], event))
                await response.write_eof()
                return response
            return web.json_response(final_response)
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Anthropic Messages bridge failed: %s", type(exc).__name__)
        return _error("Anthropic Messages service is unavailable", "upstream_error", 502)
    except Exception as exc:  # noqa: BLE001
        log.warning("Anthropic Messages bridge failed: %s", type(exc).__name__)
        return _error("Anthropic Responses conversion failed", "server_error", 502)
