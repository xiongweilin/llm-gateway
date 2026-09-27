#!/usr/bin/env python3
"""Emit Windows-netstat-shaped TCP listener rows from Linux /proc.

The conformance suite historically inspects `netstat -ano -p tcp` output on
Windows. GitHub-hosted CI runs Linux, so this adapter exposes the same factual
listener data shape without weakening the loopback-only assertion.
"""
from __future__ import annotations

import socket
from pathlib import Path


def ipv4_from_proc(hex_value: str) -> str:
    return socket.inet_ntoa(bytes.fromhex(hex_value)[::-1])


def main() -> None:
    table = Path("/proc/net/tcp")
    if not table.is_file():
        raise SystemExit("/proc/net/tcp is unavailable")

    for line in table.read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":  # TCP_LISTEN（监听状态）
            continue
        local_hex, port_hex = fields[1].split(":", 1)
        address = ipv4_from_proc(local_hex)
        port = int(port_hex, 16)
        print(f"TCP {address}:{port} 0.0.0.0:0 LISTENING 0")


if __name__ == "__main__":
    main()
