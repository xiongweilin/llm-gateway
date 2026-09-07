# -*- coding: utf-8 -*-
"""pytest fixtures：fake provider 服务 + LiteLLM 代理子进程（Windows）。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from fake_provider import FakeProviderServer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
RUN_DIR = PROJECT_ROOT / "tests" / ".run"
PROXY_LOG = RUN_DIR / "litellm-proxy.log"
EVIDENCE_JSON = RUN_DIR / "evidence.json"

MASTER_KEY = "sk-fake-canary-1234"          # 合成 master key，仅测试使用
FAKE_PROVIDER_KEY = "sk-fake-canary-5678"   # 合成 provider key，仅测试使用


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001
            proc.kill()


@pytest.fixture(scope="session")
def fake_provider() -> FakeProviderServer:
    server = FakeProviderServer().start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def proxy(fake_provider: FakeProviderServer) -> dict[str, object]:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = dict(os.environ)
    env["LITELLM_MASTER_KEY"] = MASTER_KEY
    env["FAKE_PROVIDER_API_KEY"] = FAKE_PROVIDER_KEY
    env["FAKE_PROVIDER_API_BASE"] = fake_provider.api_base

    # Invoke the repository entry directly.  Console-script trampolines can
    # retain an absolute path from a previous checkout location after the
    # workspace is moved, while the active venv Python remains valid.
    server_entry = PROJECT_ROOT / "run_server.py"
    cmd = [
        sys.executable,
        str(server_entry),
        "--config",
        str(CONFIG_PATH),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--telemetry",
        "False",
    ]

    log_handle = open(PROXY_LOG, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 240
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_handle.flush()
            raise RuntimeError(
                f"LiteLLM 代理子进程提前退出，退出码={proc.returncode}，日志见 {PROXY_LOG}"
            )
        try:
            with httpx.Client(timeout=2.0) as client:
                r = client.get(f"{base_url}/health/liveliness")
            if r.status_code == 200:
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(2)
    else:
        log_handle.flush()
        _stop_process(proc)
        raise RuntimeError(f"LiteLLM 代理 240s 内未就绪，最后错误={last_err!r}，日志见 {PROXY_LOG}")

    info: dict[str, object] = {
        "base_url": base_url,
        "port": port,
        "proc": proc,
        "log_path": PROXY_LOG,
        "config_path": CONFIG_PATH,
    }
    try:
        yield info
    finally:
        _stop_process(proc)
        log_handle.close()


@pytest.fixture(autouse=True)
def _reset_fake_provider(fake_provider: FakeProviderServer):
    fake_provider.reset()
    yield


@pytest.fixture()
def client() -> httpx.Client:
    # trust_env=False：不走系统 HTTP 代理，直连 127.0.0.1
    return httpx.Client(timeout=60.0, trust_env=False)


@pytest.fixture(scope="session", autouse=True)
def _dump_evidence_on_finish():
    from evidence import dump_evidence

    yield
    try:
        dump_evidence(EVIDENCE_JSON)
    except Exception:  # noqa: BLE001
        pass
