# -*- coding: utf-8 -*-
"""OpenAI-compatible fake provider for routing-core integration tests.

实现：
  * POST /v1/responses        —— 非流式 JSON 与 SSE 流式（含 reasoning 与 tool_calls 输出）
  * POST /v1/chat/completions —— 非流式与 SSE 流式

特性：
  * 记录每个 outbound 请求的原始 body（消息/工具顺序、动态字段、cache 参数），供断言使用
  * 记录流被客户端中断（连接断开）的事件，用于验证取消语义传播

全部内容为合成 canary 数据，不涉及任何真实凭据/正文。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CANARY_CACHED_TOKENS = 1234
REASONING_TEXT = "canary reasoning summary"


class FakeProviderError(Exception):
    pass


class RequestRegistry:
    """线程安全的请求记录器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []
        self.abort_events: list[dict[str, Any]] = []
        self._counter = 0

    def reset(self) -> None:
        with self._lock:
            self.records.clear()
            self.abort_events.clear()
            self._counter = 0

    def add(self, path: str, headers: dict[str, str], raw_body: bytes, body: Any) -> None:
        with self._lock:
            self._counter += 1
            self.records.append(
                {
                    "index": self._counter,
                    "path": path,
                    "t_monotonic": time.monotonic(),
                    "t_wall": time.time(),
                    "auth_header_present": "authorization" in headers,
                    "auth_scheme": (headers.get("authorization", "").split(" ", 1)[0] if headers.get("authorization") else ""),
                    "content_type": headers.get("content-type", ""),
                    "raw_body": raw_body.decode("utf-8", errors="replace"),
                    "body": body,
                }
            )

    def add_abort(self, path: str) -> None:
        with self._lock:
            self.abort_events.append({"path": path, "t_monotonic": time.monotonic(), "t_wall": time.time()})

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.records)

    def aborts(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.abort_events)


REGISTRY = RequestRegistry()


def _sse_frame(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _build_usage(cached_tokens: int = CANARY_CACHED_TOKENS) -> dict[str, Any]:
    return {
        "input_tokens": 7,
        "output_tokens": 5,
        "total_tokens": 12,
        "reasoning_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 3, "text_tokens": 2},
        "input_tokens_details": {"cached_tokens": cached_tokens, "text_tokens": 4, "image_tokens": 0},
    }


def _extract_texts(input_value: Any) -> list[str]:
    """从 Responses API 的 input（str 或 item 列表）中按顺序提取文本。"""
    texts: list[str] = []
    if isinstance(input_value, str):
        texts.append(input_value)
        return texts
    if not isinstance(input_value, list):
        return texts
    for item in input_value:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts


def _last_user_text(input_value: Any) -> str:
    texts = _extract_texts(input_value)
    return texts[-1] if texts else "canary-default"


class FakeProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_aborted_write(self, path: str) -> None:
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            REGISTRY.add_abort(path)

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": {"type": "not_found", "message": "unknown path"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read_body()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}

        REGISTRY.add(
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            raw_body=raw,
            body=body,
        )

        if self.path.endswith("/responses") or self.path == "/responses":
            self._handle_responses(body)
        elif self.path.endswith("/chat/completions"):
            self._handle_chat_completions(body)
        else:
            self._send_json(404, {"error": {"type": "not_found", "message": f"unknown path {self.path}"}})

    # ---- Responses API 路由 ----
    def _handle_responses(self, body: dict[str, Any]) -> None:
        if body.get("stream"):
            self._stream_responses(body)
        else:
            self._send_json(200, self._build_responses_object(body))

    def _build_responses_object(self, body: dict[str, Any]) -> dict[str, Any]:
        tools = body.get("tools") or []
        instructions = body.get("instructions") or ""
        texts = _extract_texts(body.get("input"))
        want_tool_call = bool(tools) and ("CALL_TOOL" in " ".join(texts) or "CALL_TOOL" in instructions)
        output: list[dict[str, Any]] = []

        # reasoning 条目
        output.append(
            {
                "id": "rs_canary_1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": REASONING_TEXT}],
            }
        )
        # function call item（触发时）
        if want_tool_call:
            tool_name = tools[0]["name"] if isinstance(tools[0], dict) and "name" in tools[0] else "canary_tool_alpha"
            output.append(
                {
                    "id": "fc_canary_1",
                    "type": "function_call",
                    "call_id": "call_canary_1",
                    "name": tool_name,
                    "arguments": '{"q": "canary-arg"}',
                }
            )
        # message 条目
        output.append(
            {
                "id": "msg_canary_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"echo: {_last_user_text(body.get('input'))}",
                        "annotations": [],
                    }
                ],
            }
        )

        return {
            "id": "resp_canary_1",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": body.get("model", "fake-responses"),
            "output": output,
            "usage": _build_usage(),
        }

    def _stream_responses(self, body: dict[str, Any]) -> None:
        tools = body.get("tools") or []
        texts = _extract_texts(body.get("input"))
        instructions = body.get("instructions") or ""
        want_tool_call = bool(tools) and ("CALL_TOOL" in " ".join(texts) or "CALL_TOOL" in instructions)
        echo_text = f"echo: {_last_user_text(body.get('input'))}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        base = {
            "object": "response",
            "model": body.get("model", "fake-responses"),
            "output": [],
            "parallel_tool_calls": True,
            "tools": tools,
        }
        created = {"id": "resp_canary_stream_1", "created_at": int(time.time()), "status": "in_progress", **base}

        events: list[tuple[str, dict[str, Any]]] = [
            ("response.created", {"type": "response.created", "response": created}),
            ("response.in_progress", {"type": "response.in_progress", "response": created}),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "rs_canary_1", "type": "reasoning", "summary": []},
                },
            ),
            (
                "response.reasoning_summary_text.delta",
                {"type": "response.reasoning_summary_text.delta", "item_id": "rs_canary_1", "output_index": 0, "delta": REASONING_TEXT},
            ),
            (
                "response.reasoning_summary_text.done",
                {"type": "response.reasoning_summary_text.done", "item_id": "rs_canary_1", "output_index": 0, "text": REASONING_TEXT},
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": "rs_canary_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": REASONING_TEXT}]},
                },
            ),
        ]

        if want_tool_call:
            tool_name = tools[0]["name"] if isinstance(tools[0], dict) and "name" in tools[0] else "canary_tool_alpha"
            events.extend(
                [
                    (
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": 1,
                            "item": {"id": "fc_canary_1", "type": "function_call", "call_id": "call_canary_1", "name": tool_name, "arguments": ""},
                        },
                    ),
                    (
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc_canary_1",
                            "output_index": 1,
                            "delta": '{"q": "canary-arg"}',
                        },
                    ),
                    (
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": "fc_canary_1",
                            "output_index": 1,
                            "arguments": '{"q": "canary-arg"}',
                        },
                    ),
                    (
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "output_index": 1,
                            "item": {
                                "id": "fc_canary_1",
                                "type": "function_call",
                                "call_id": "call_canary_1",
                                "name": tool_name,
                                "arguments": '{"q": "canary-arg"}',
                            },
                        },
                    ),
                ]
            )

        msg_index = 2 if want_tool_call else 1
        completed_output = [
            data["item"] for event, data in events if event == "response.output_item.done"
        ]
        events.extend(
            [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": msg_index,
                        "item": {"id": "msg_canary_1", "type": "message", "role": "assistant", "content": []},
                    },
                ),
                (
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": "msg_canary_1",
                        "output_index": msg_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                ),
                (
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "item_id": "msg_canary_1", "output_index": msg_index, "content_index": 0, "delta": echo_text},
                ),
                (
                    "response.output_text.done",
                    {"type": "response.output_text.done", "item_id": "msg_canary_1", "output_index": msg_index, "content_index": 0, "text": echo_text},
                ),
                (
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": "msg_canary_1",
                        "output_index": msg_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": echo_text, "annotations": []},
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": msg_index,
                        "item": {
                            "id": "msg_canary_1",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": echo_text, "annotations": []}],
                        },
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            **created,
                            "status": "completed",
                            "output": completed_output,
                            "usage": _build_usage(),
                        },
                    },
                ),
            ]
        )

        try:
            for event, data in events:
                self.wfile.write(_sse_frame(event, data))
                self.wfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            REGISTRY.add_abort(self.path)
            return
        except OSError:
            REGISTRY.add_abort(self.path)
            return
        finally:
            self.close_connection = True

    # ---- Chat Completions 路由 ----
    def _handle_chat_completions(self, body: dict[str, Any]) -> None:
        if body.get("stream"):
            self._stream_chat(body)
        else:
            self._send_json(200, self._build_chat_object(body))

    def _build_chat_object(self, body: dict[str, Any]) -> dict[str, Any]:
        messages = body.get("messages") or []
        texts = [m.get("content") for m in messages if isinstance(m, dict) and isinstance(m.get("content"), str)]
        reply = texts[-1] if texts else "canary-default"
        return {
            "id": "chatcmpl_canary_1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake-responses"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"echo: {reply}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 5,
                "total_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": CANARY_CACHED_TOKENS},
            },
        }

    def _stream_chat(self, body: dict[str, Any]) -> None:
        messages = body.get("messages") or []
        texts = [m.get("content") for m in messages if isinstance(m, dict) and isinstance(m.get("content"), str)]
        reply = texts[-1] if texts else "canary-default"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {
                "id": "chatcmpl_canary_stream_1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-responses"),
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "echo: "}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_canary_stream_1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-responses"),
                "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_canary_stream_1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-responses"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        try:
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(0.04)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            REGISTRY.add_abort(self.path)
        except OSError:
            REGISTRY.add_abort(self.path)
        finally:
            self.close_connection = True


class FakeProviderServer:
    """在线程中运行 fake provider 的 HTTP 服务。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._httpd = ThreadingHTTPServer((host, port), FakeProviderHandler)
        self._httpd.daemon_threads = True
        self._httpd.allow_reuse_address = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="fake-provider")

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/v1"

    def start(self) -> "FakeProviderServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def reset(self) -> None:
        REGISTRY.reset()

    def records(self) -> list[dict[str, Any]]:
        return REGISTRY.snapshot()

    def aborts(self) -> list[dict[str, Any]]:
        return REGISTRY.aborts()


def start_server() -> FakeProviderServer:
    return FakeProviderServer().start()
