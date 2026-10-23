from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows startup proxy environment")
PROXY_SCRIPT = Path(__file__).parents[2] / "scripts" / "gateway-proxy.ps1"


def _initialize_proxy(settings, configured=None):
    env = os.environ.copy()
    for name in list(env):
        if name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}:
            del env[name]
    env.update(configured or {})
    env["GATEWAY_TEST_PROXY_SCRIPT"] = str(PROXY_SCRIPT)
    env["GATEWAY_TEST_PROXY_SETTINGS"] = json.dumps(settings)
    result = subprocess.run(
        [shutil.which("pwsh.exe") or "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. $env:GATEWAY_TEST_PROXY_SCRIPT
$settings = $env:GATEWAY_TEST_PROXY_SETTINGS | ConvertFrom-Json
Initialize-GatewayProxyEnvironment -InternetSettings $settings
$first = @($env:HTTP_PROXY, $env:HTTPS_PROXY, $env:NO_PROXY) | ConvertTo-Json -Compress
Initialize-GatewayProxyEnvironment -InternetSettings $settings
$second = @($env:HTTP_PROXY, $env:HTTPS_PROXY, $env:NO_PROXY) | ConvertTo-Json -Compress
if ($first -ne $second) { throw 'proxy initialization is not idempotent' }
@{HTTP_PROXY=$env:HTTP_PROXY; HTTPS_PROXY=$env:HTTPS_PROXY; NO_PROXY=$env:NO_PROXY} | ConvertTo-Json -Compress
"""],
        env=env, text=True, capture_output=True, check=True, timeout=20,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("configured", [
    {"HTTP_PROXY": "http://explicit-http.invalid:9000"},
    {"HTTPS_PROXY": "http://explicit-https.invalid:9001"},
    {"HTTP_PROXY": "http://explicit-http.invalid:9000", "HTTPS_PROXY": "http://explicit-https.invalid:9001"},
    {},
])
def test_proxy_protocol_mapping_preserves_each_explicit_override(configured):
    actual = _initialize_proxy(
        {"ProxyEnable": 1, "ProxyServer": "http=system-http.invalid:8000;https=system-https.invalid:8001"},
        configured,
    )
    assert actual["HTTP_PROXY"] == configured.get("HTTP_PROXY", "http://system-http.invalid:8000")
    assert actual["HTTPS_PROXY"] == configured.get("HTTPS_PROXY", "http://system-https.invalid:8001")


@pytest.mark.parametrize("settings,expected_http,expected_https", [
    ({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8000"}, "http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ({"ProxyEnable": 1, "ProxyServer": "http=http://proxy.invalid:8000"}, "http://proxy.invalid:8000", None),
    ({"ProxyEnable": 1, "ProxyServer": "socks=127.0.0.1:8000"}, None, None),
    ({"ProxyEnable": 0, "ProxyServer": "127.0.0.1:8000"}, None, None),
    ({}, None, None),
    ({"ProxyEnable": 1}, None, None),
])
def test_system_proxy_shapes(settings, expected_http, expected_https):
    actual = _initialize_proxy(settings)
    assert actual["HTTP_PROXY"] == expected_http
    assert actual["HTTPS_PROXY"] == expected_https


@pytest.mark.parametrize("bypass", ["127.0.0.1", "localhost", "example.invalid, ::1", ""])
def test_each_loopback_bypass_is_completed_independently(bypass):
    actual = _initialize_proxy({"ProxyEnable": 0}, {"NO_PROXY": bypass})
    entries = actual["NO_PROXY"].split(',')
    for address in ("127.0.0.1", "localhost", "::1"):
        assert entries.count(address) == 1
    assert set(part.strip() for part in bypass.split(',') if part.strip()).issubset(entries)
