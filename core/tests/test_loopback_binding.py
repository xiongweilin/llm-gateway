from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

MODELS_CONFIG = Path(__file__).parents[1] / "models.yaml"
HEALTH_URL_TEMPLATE = "http://127.0.0.1:{port}/health/liveliness"
READY_DEADLINE_SECONDS = 30.0
REACHABILITY_TIMEOUT_SECONDS = 0.5


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _non_loopback_ipv4s() -> list[str]:
    candidates: set[str] = set()
    for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        address = result[4][0]
        if not address.startswith("127."):
            candidates.add(address)
    return sorted(candidates)


def test_core_server_launched_on_loopback_is_unreachable_via_non_loopback_interfaces() -> None:
    """Deployment contract: the core listens on 127.0.0.1 only, verified by reachability."""
    assert MODELS_CONFIG.is_file()
    port = _pick_free_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "llm_gateway.core_server",
            "--config",
            str(MODELS_CONFIG),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        health_url = HEALTH_URL_TEMPLATE.format(port=port)
        deadline = time.monotonic() + READY_DEADLINE_SECONDS
        last_error: Exception | None = None
        probe = httpx.Client(trust_env=False)
        try:
            ready = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stderr = process.stderr.read(2048).decode(errors="replace") if process.stderr else ""
                    pytest.fail(f"core server exited early (rc={process.returncode}): {stderr}")
                try:
                    response = probe.get(health_url, timeout=1.0)
                except httpx.HTTPError as exc:
                    last_error = exc
                    time.sleep(0.25)
                    continue
                if response.status_code == 200:
                    ready = True
                    break
                time.sleep(0.25)
            assert ready, f"core server never became ready on 127.0.0.1:{port}; last error: {last_error}"
        finally:
            probe.close()

        non_loopback = _non_loopback_ipv4s()
        if not non_loopback:
            pytest.skip("host has no non-loopback IPv4 address to probe")
        reachable: list[str] = []
        for address in non_loopback:
            try:
                with socket.create_connection((address, port), timeout=REACHABILITY_TIMEOUT_SECONDS):
                    reachable.append(address)
            except OSError:
                continue
        assert not reachable, f"core listener is reachable via non-loopback interfaces: {reachable}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
