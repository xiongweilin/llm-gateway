# -*- coding: utf-8 -*-
"""LiteLLM 网关 conformance 测试（fake provider）。

测试只使用合成 canary 内容与合成 key（sk-fake-canary-*），不涉及真实凭据/正文。
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from typing import Any, Iterable

import httpx
import pytest

from conftest import FAKE_PROVIDER_KEY, MASTER_KEY, free_port
from evidence import record
from fake_provider import CANARY_CACHED_TOKENS, REASONING_TEXT, FakeProviderServer

AUTH = {"Authorization": f"Bearer {MASTER_KEY}"}

MARKERS = ["SYS_CANARY_0", "USER_CANARY_1", "USER_CANARY_2"]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _walk_strings(obj: Any, prefixes: tuple[str, ...]) -> list[str]:
    """按文档顺序收集 JSON 中所有以 prefixes 开头的字符串。"""
    found: list[str] = []
    if isinstance(obj, str):
        if obj.startswith(prefixes):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_walk_strings(v, prefixes))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk_strings(v, prefixes))
    return found


def _records_for(server: FakeProviderServer, path_suffix: str) -> list[dict[str, Any]]:
    return [r for r in server.records() if r["path"].endswith(path_suffix)]


def _input_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("input")
    if isinstance(items, str):
        return []
    return [i for i in items if isinstance(i, dict)]


def _parse_sse(lines: Iterable[str]) -> list[tuple[str, str]]:
    """解析 SSE：事件名优先取 `event:` 行；否则取 data JSON 的 `type` 字段。

    litellm 代理下发的 /v1/responses 流不含 `event:` 行，事件类型在 data 内。
    """
    events: list[tuple[str, str]] = []
    ev: str | None = None
    data_parts: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            ev = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_parts.append(line[len("data:"):].strip())
        elif line == "" and (ev is not None or data_parts):
            data_str = "\n".join(data_parts)
            if data_str == "[DONE]":
                ev, data_parts = None, []
                continue
            if ev is None and data_str.startswith("{"):
                try:
                    ev = (json.loads(data_str) or {}).get("type") or "message"
                except json.JSONDecodeError:
                    ev = "message"
            events.append((ev or "message", data_str))
            ev, data_parts = None, []
    return events


def _non_loopback_ipv4s() -> list[str]:
    ips: set[str] = set()
    for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = res[4][0]
        if not ip.startswith("127."):
            ips.add(ip)
    return sorted(ips)


def _listening_addresses(port: int) -> list[str]:
    out = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    addrs: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING":
            local = parts[1]
            if local.rsplit(":", 1)[-1] == str(port):
                addrs.append(local)
    return addrs


# --------------------------------------------------------------------------
# A. 消息 / 工具顺序保持
# --------------------------------------------------------------------------
def test_01_message_and_tool_order_preserved(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload: dict[str, Any] = {
        "model": "fake-responses",
        "instructions": "INSTR_CANARY_0",
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "SYS_CANARY_0"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "USER_CANARY_1"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "USER_CANARY_2"}]},
        ],
        "tools": [
            {
                "type": "function",
                "name": "canary_tool_alpha",
                "description": "ALPHA_DESC_CANARY",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            },
            {"type": "function", "name": "canary_tool_beta", "description": "BETA_DESC_CANARY", "parameters": {"type": "object", "properties": {}}},
        ],
    }

    r = client.post(f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH)
    assert r.status_code == 200, f"代理返回非 200: {r.status_code} {r.text[:500]}"

    # 客户端侧响应结构
    resp = r.json()
    assert resp.get("id", "").startswith("resp_"), resp
    assert resp.get("status") == "completed", resp
    out_types = [o.get("type") for o in resp.get("output", [])]
    assert out_types == ["reasoning", "message"], f"output 类型异常: {out_types}"
    msg_outputs = [o for o in resp.get("output", []) if o.get("type") == "message"]
    assert "echo: USER_CANARY_2" in json.dumps(msg_outputs, ensure_ascii=False)

    # 上游收到的请求：顺序与内容一致、无重排、无额外注入
    recs = _records_for(fake_provider, "/v1/responses")
    assert len(recs) == 1, f"期望 1 条 /v1/responses 上游请求，实际 {len(recs)}"
    body = recs[0]["body"]

    texts = _walk_strings(body, tuple(MARKERS))
    assert texts == MARKERS, f"消息文本顺序被改变或注入: {texts}"

    items = _input_items(body)
    roles = [i.get("role") for i in items]
    assert roles == ["system", "user", "user"], f"role 顺序异常: {roles}"
    assert len(items) == 3, f"消息条数异常（不应额外注入消息）: {len(items)}"

    tool_names = _walk_strings(body, ("canary_tool_alpha", "canary_tool_beta"))
    assert tool_names == ["canary_tool_alpha", "canary_tool_beta"], f"工具顺序异常: {tool_names}"
    descs = _walk_strings(body, ("ALPHA_DESC_CANARY", "BETA_DESC_CANARY"))
    assert descs == ["ALPHA_DESC_CANARY", "BETA_DESC_CANARY"]

    # 消息内部不允许出现时间戳 / request_id 类动态键
    for item in items:
        for key in item.keys():
            assert "timestamp" not in key.lower() and "request_id" not in key.lower(), f"消息内出现动态键: {key}"

    record(
        "A1_messages_tools_order",
        "pass",
        {
            "upstream_path": recs[0]["path"],
            "markers_order": texts,
            "roles": roles,
            "tool_names": tool_names,
            "item_keys": [sorted(i.keys()) for i in items],
            "response_output_types": out_types,
        },
    )


# --------------------------------------------------------------------------
# B. 稳定前缀无动态字节
# --------------------------------------------------------------------------
def test_02_stable_prefix_no_dynamic_bytes(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload: dict[str, Any] = {
        "model": "fake-responses",
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "SYS_CANARY_0"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "USER_CANARY_1"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "USER_CANARY_2"}]},
        ],
        "tools": [
            {
                "type": "function",
                "name": "canary_tool_alpha",
                "description": "ALPHA_DESC_CANARY",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            },
            {"type": "function", "name": "canary_tool_beta", "description": "BETA_DESC_CANARY", "parameters": {"type": "object", "properties": {}}},
        ],
    }

    for _ in range(2):
        r = client.post(f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH)
        assert r.status_code == 200, f"代理返回非 200: {r.status_code} {r.text[:500]}"

    recs = _records_for(fake_provider, "/v1/responses")
    assert len(recs) == 2, f"期望 2 条上游请求，实际 {len(recs)}"
    b1, b2 = recs[0]["body"], recs[1]["body"]

    # 消息数组内部必须字节一致
    in1 = json.dumps(b1["input"], sort_keys=True, ensure_ascii=True)
    in2 = json.dumps(b2["input"], sort_keys=True, ensure_ascii=True)
    assert in1 == in2, "两次请求的消息数组（稳定前缀）存在动态字节"
    t1 = json.dumps(b1.get("tools"), sort_keys=True, ensure_ascii=True)
    t2 = json.dumps(b2.get("tools"), sort_keys=True, ensure_ascii=True)
    assert t1 == t2, "两次请求的 tools 存在动态字节"

    # 整包比较：仅允许顶层 id/时间戳类键不同
    def normalized(d: dict[str, Any]) -> dict[str, Any]:
        out = dict(d)
        for k in ("id", "created_at", "created", "timestamp", "request_id", "litellm_call_id"):
            out.pop(k, None)
        return out

    assert json.dumps(normalized(b1), sort_keys=True, ensure_ascii=True) == json.dumps(
        normalized(b2), sort_keys=True, ensure_ascii=True
    ), "除顶层动态键外，两次请求的原始 body 应一致"

    # 消息条目内部的键必须稳定
    for item in b1["input"]:
        for key in item.keys():
            assert "timestamp" not in key.lower() and "request_id" not in key.lower(), f"消息内出现动态键: {key}"

    record(
        "B1_stable_prefix",
        "pass",
        {
            "input_bytes_identical": in1 == in2,
            "tools_bytes_identical": t1 == t2,
            "normalized_full_body_identical": json.dumps(normalized(b1), sort_keys=True) == json.dumps(
                normalized(b2), sort_keys=True
            ),
            "top_level_keys_body1": sorted(b1.keys()),
        },
    )


# --------------------------------------------------------------------------
# C. cache 参数转发与 usage 标准化（/v1/responses）
# --------------------------------------------------------------------------
def test_03_cache_params_and_usage_mapping(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload: dict[str, Any] = {
        "model": "fake-responses",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "CACHE_CANARY_MSG", "caching": {"ephemeral": True}}],
            }
        ],
        "prompt_cache_key": "canary-cache-key-1",
        "prompt_cache_retention": "5m",
        "prompt_cache_options": {"type": "ephemeral", "ttl": "5m"},  # 观察：是否被转发
    }

    r = client.post(f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH)
    assert r.status_code == 200, f"代理返回非 200: {r.status_code} {r.text[:500]}"
    resp = r.json()

    usage = resp.get("usage") or {}
    cached_fields = {
        "input_tokens_details.cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        "prompt_tokens_details.cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }
    present = {k: v for k, v in cached_fields.items() if v is not None}
    assert present, f"响应 usage 中未找到任何 cache 类字段: {usage}"
    assert cached_fields["input_tokens_details.cached_tokens"] == CANARY_CACHED_TOKENS, (
        f"cached_tokens 映射异常: {cached_fields}"
    )

    # 上游实际收到的 cache 参数（如实记录）
    recs = _records_for(fake_provider, "/v1/responses")
    assert len(recs) == 1
    up = recs[0]["body"]
    up_input_items = _input_items(up)
    up_caching = None
    if up_input_items:
        content = up_input_items[0].get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            up_caching = content[0].get("caching")
    forwarding = {
        "prompt_cache_key_forwarded": up.get("prompt_cache_key"),
        "prompt_cache_retention_forwarded": up.get("prompt_cache_retention"),
        "prompt_cache_options_forwarded": up.get("prompt_cache_options"),
        "input_item_caching_forwarded": up_caching,
    }
    record(
        "C1_cache_params_and_usage",
        "pass",
        {"usage_fields_present": present, "forwarding": forwarding},
    )


# --------------------------------------------------------------------------
# F. usage 标准化（/v1/chat/completions）
# --------------------------------------------------------------------------
def test_04_chat_usage_cache_mapping(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload: dict[str, Any] = {
        "model": "fake-responses",
        "messages": [
            {"role": "system", "content": "CHAT_SYS_CANARY"},
            {"role": "user", "content": "CHAT_USER_CANARY"},
        ],
    }
    r = client.post(f"{proxy['base_url']}/v1/chat/completions", json=payload, headers=AUTH)
    assert r.status_code == 200, f"代理返回非 200: {r.status_code} {r.text[:500]}"
    resp = r.json()

    usage = resp.get("usage") or {}
    cached_fields = {
        "prompt_tokens_details.cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }
    present = {k: v for k, v in cached_fields.items() if v is not None}
    assert present, f"chat 响应 usage 中未找到任何 cache 类字段: {usage}"
    assert cached_fields["prompt_tokens_details.cached_tokens"] == CANARY_CACHED_TOKENS, cached_fields

    recs = _records_for(fake_provider, "/v1/chat/completions")
    assert len(recs) == 1
    up_messages = recs[0]["body"].get("messages", [])
    texts = _walk_strings(recs[0]["body"], ("CHAT_SYS_CANARY", "CHAT_USER_CANARY"))
    assert texts == ["CHAT_SYS_CANARY", "CHAT_USER_CANARY"], f"chat 消息顺序异常: {texts}"

    record(
        "F1_chat_usage_cache_mapping",
        "pass",
        {"usage_fields_present": present, "chat_markers_order": texts, "messages_len": len(up_messages)},
    )


# --------------------------------------------------------------------------
# D. streaming：事件顺序 / reasoning / tool_calls / 取消
# --------------------------------------------------------------------------
def test_05_streaming_events_reasoning_tool_calls(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload: dict[str, Any] = {
        "model": "fake-responses",
        "stream": True,
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "STREAM_CALL_TOOL_CANARY"}]},
        ],
        "tools": [
            {"type": "function", "name": "canary_tool_alpha", "description": "ALPHA_DESC_CANARY", "parameters": {"type": "object", "properties": {}}}
        ],
    }
    with client.stream("POST", f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH) as r:
        assert r.status_code == 200, f"流式请求非 200: {r.status_code}"
        events = _parse_sse(r.iter_lines())

    assert events, "未收到任何 SSE 事件"
    types = [ev for ev, _ in events]
    assert types[0] in ("response.created", "response.in_progress"), f"首个事件异常: {types[0]}"
    assert types[-1] == "response.completed", f"末事件应为 response.completed: {types[-1]}"

    all_data = " ".join(d for _, d in events)
    assert REASONING_TEXT in all_data, "流中未出现 reasoning 文本"
    assert any("function_call" in t for t in types) or "canary_tool_alpha" in all_data, "流中未出现 tool_calls"
    assert "echo: STREAM_CALL_TOOL_CANARY" in all_data, "流中未出现消息正文"

    completed = [json.loads(d) for ev, d in events if ev == "response.completed"]
    assert completed and (completed[-1].get("response", {}).get("status") == "completed")
    completed_output_types = [
        o.get("type") for o in (completed[-1].get("response", {}).get("output") or [])
    ]

    record(
        "D1_streaming_events",
        "pass",
        {
            "event_types": types,
            "n_events": len(events),
            "completed_output_item_types": completed_output_types,
        },
    )


def test_06_stream_cancellation_propagates(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload: dict[str, Any] = {
        "model": "fake-responses",
        "stream": True,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "CANCEL_CANARY_MSG"}]}],
    }
    got: list[str] = []
    with client.stream("POST", f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:") and not line.startswith("data: [DONE]"):
                try:
                    got.append((json.loads(line[len("data:"):].strip()) or {}).get("type", "?"))
                except json.JSONDecodeError:
                    got.append("?")
                if len(got) >= 2:
                    break  # 客户端主动中断

    assert got, "流式请求未收到任何事件"
    # 上游应观察到连接被中断（取消传播）
    deadline = time.monotonic() + 20
    aborts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        aborts = [a for a in fake_provider.aborts() if a["path"].endswith("/v1/responses")]
        if aborts:
            break
        time.sleep(0.5)

    record(
        "D2_stream_cancellation",
        "pass" if aborts else "fail",
        {"client_events_seen": got, "upstream_abort_events": aborts},
    )
    assert aborts, "客户端断开后，上游 fake provider 未观察到连接中断（取消未传播）"


# --------------------------------------------------------------------------
# E. 认证与绑定
# --------------------------------------------------------------------------
def test_07_auth_missing_and_wrong_key(client: httpx.Client, proxy: dict[str, object]):
    payload = {"model": "fake-responses", "input": "AUTH_CANARY_MSG"}
    r_no = client.post(f"{proxy['base_url']}/v1/responses", json=payload)
    assert r_no.status_code == 401, f"无 key 应 401，实际 {r_no.status_code}: {r_no.text[:300]}"

    r_wrong = client.post(
        f"{proxy['base_url']}/v1/responses", json=payload, headers={"Authorization": "Bearer sk-wrong-canary-9999"}
    )
    # litellm DB-less 模式固有行为：任何非 master key（含错 key）在无数据库时
    # 命中 `prisma_client is None -> ProxyException("No connected db.", code=400)`。
    # 请求被拒绝（400/401 均视为拒绝），状态码如实记录为偏差，见 CONFORMANCE.md。
    assert r_wrong.status_code in (400, 401), f"错 key 应被拒绝(400/401)，实际 {r_wrong.status_code}: {r_wrong.text[:300]}"

    r_ok = client.post(f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH)
    assert r_ok.status_code == 200, f"正确 key 应 200，实际 {r_ok.status_code}: {r_ok.text[:300]}"

    r_models = client.get(f"{proxy['base_url']}/v1/models", headers={"Authorization": "Bearer sk-wrong-canary-9999"})
    assert r_models.status_code in (400, 401), f"错 key 访问 /v1/models 应被拒绝，实际 {r_models.status_code}"

    record(
        "E1_auth",
        "pass" if r_wrong.status_code == 401 else "pass_with_deviation",
        {
            "no_key_status": r_no.status_code,
            "wrong_key_status": r_wrong.status_code,
            "wrong_key_body": json.loads(r_wrong.text).get("error", {}).get("message"),
            "correct_key_status": r_ok.status_code,
            "models_wrong_key_status": r_models.status_code,
            "deviation": "DB-less 下非 master key 一律 400 no_db_connection（litellm 1.96.0 固有行为），无法返回 401",
        },
    )


def test_08_loopback_binding_only(client: httpx.Client, proxy: dict[str, object]):
    port = proxy["port"]
    listening = _listening_addresses(port)
    assert listening, f"未找到端口 {port} 的监听记录"
    assert all(a.startswith("127.0.0.1:") for a in listening), f"存在非 loopback 监听: {listening}"
    assert any(a == f"127.0.0.1:{port}" for a in listening), f"未监听 127.0.0.1:{port}: {listening}"

    non_loopback = _non_loopback_ipv4s()
    if not non_loopback:
        record("E2_binding", "pass", {"listening": listening, "non_loopback_check": "skipped: no non-loopback IPv4"})
        pytest.skip("本机没有非 loopback IPv4 地址，跳过可达性负测试")

    reachable: list[str] = []
    for ip in non_loopback:
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as c:
                resp = c.get(f"http://{ip}:{port}/health/liveliness")
            if resp.status_code == 200:
                reachable.append(ip)  # 意外可达
        except httpx.ConnectError:
            pass  # 连接被拒绝：不可达（符合预期）
        except httpx.TimeoutException:
            pass  # 超时：不可达（符合预期）

    assert not reachable, f"通过非 loopback 地址访问代理意外可达: {reachable}"
    record("E2_binding", "pass", {"listening": listening, "non_loopback_tried": non_loopback, "non_loopback_reachable": reachable})


# --------------------------------------------------------------------------
# 稳定别名（router_settings.model_group_alias）
# --------------------------------------------------------------------------
def test_09_stable_alias(client: httpx.Client, proxy: dict[str, object], fake_provider: FakeProviderServer):
    payload = {"model": "fake-gw", "input": [{"role": "user", "content": [{"type": "input_text", "text": "ALIAS_CANARY_MSG"}]}]}
    r = client.post(f"{proxy['base_url']}/v1/responses", json=payload, headers=AUTH)
    assert r.status_code == 200, f"别名请求非 200: {r.status_code} {r.text[:500]}"
    assert "ALIAS_CANARY_MSG" in r.text
    recs = _records_for(fake_provider, "/v1/responses")
    assert len(recs) == 1, "别名请求未到达上游 fake provider"
    record("A2_stable_alias", "pass", {"upstream_model_received": recs[0]["body"].get("model"), "alias": "fake-gw"})
