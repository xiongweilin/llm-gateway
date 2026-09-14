from __future__ import annotations

import socket

import httpx
import pytest

from evidence import record


def _non_loopback_ipv4s() -> list[str]:
    ips: set[str] = set()
    for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = result[4][0]
        if not ip.startswith("127."):
            ips.add(ip)
    return sorted(ips)


def test_loopback_binding_behavior(proxy: dict[str, object]) -> None:
    """Verify the binding contract by reachability instead of OS-specific netstat output."""
    base_url = str(proxy["base_url"])
    port = int(proxy["port"])

    with httpx.Client(timeout=3.0, trust_env=False) as client:
        response = client.get(f"{base_url}/health/liveliness")
    assert response.status_code == 200, (
        f"loopback endpoint must be reachable, got {response.status_code}"
    )

    non_loopback = _non_loopback_ipv4s()
    if not non_loopback:
        record(
            "E2_binding",
            "pass",
            {"loopback_reachable": True, "non_loopback_check": "skipped: no non-loopback IPv4"},
        )
        pytest.skip("runner has no non-loopback IPv4 address")

    reachable: list[str] = []
    for ip in non_loopback:
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                response = client.get(f"http://{ip}:{port}/health/liveliness")
            if response.status_code == 200:
                reachable.append(ip)
        except (httpx.ConnectError, httpx.TimeoutException):
            pass

    assert not reachable, f"proxy unexpectedly reachable on non-loopback addresses: {reachable}"
    record(
        "E2_binding",
        "pass",
        {
            "loopback_reachable": True,
            "non_loopback_tried": non_loopback,
            "non_loopback_reachable": reachable,
        },
    )
