#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Send one nonce-bound UDP datagram and prove the one-packet contract."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import select
import re
import socket
import sys
from pathlib import Path


PAYLOAD = b"ASTERINAS-SINGLE-UDP-V1"
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def parse_cmdline(contents: str) -> dict[str, str]:
    """Parse the single-packet bootarg namespace and reject duplicates."""

    values: dict[str, str] = {}
    for token in contents.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if not key.startswith("asterinas.single_packet_"):
            continue
        if key in values:
            raise ValueError(f"duplicate bootarg: {key}")
        values[key] = value
    return values


def _validate(values: dict[str, str]) -> tuple[str, str, int]:
    nonce = values.get("asterinas.single_packet_nonce", "")
    peer = values.get("asterinas.single_packet_peer", "")
    port_text = values.get("asterinas.single_packet_port", "")
    if not NONCE_RE.fullmatch(nonce):
        raise ValueError("invalid single-packet nonce")
    try:
        ipaddress.ip_address(peer)
        port = int(port_text, 10)
    except ValueError as error:
        raise ValueError("invalid single-packet peer or port") from error
    if ":" in peer or not 1 <= port <= 65535:
        raise ValueError("single-packet endpoint must be IPv4 and in range")
    return nonce, peer, port


def _record(
    console: Path,
    nonce: str,
    *,
    tx: int,
    rx: int,
    tx_errors: int,
    rx_errors: int,
    payload_sha256: str,
    status: str,
) -> None:
    line = (
        "ASTERINAS_GMAC_SINGLE_PACKET "
        f"nonce={nonce} tx={tx} rx={rx} tx_errors={tx_errors} "
        f"rx_errors={rx_errors} payload_sha256={payload_sha256} status={status}\n"
    )
    with console.open("a", encoding="ascii") as stream:
        stream.write(line)


def run(*, cmdline: Path, console: Path, timeout: float) -> int:
    nonce = "0" * 32
    digest = hashlib.sha256(b"").hexdigest()
    tx = rx = 0
    try:
        values = parse_cmdline(cmdline.read_text(encoding="ascii"))
        nonce, peer, port = _validate(values)
        payload = nonce.encode("ascii") + b":" + PAYLOAD
        digest = hashlib.sha256(payload).hexdigest()
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            if sock.sendto(payload, (peer, port)) != len(payload):
                raise ValueError("short UDP send")
            tx = 1
            response, address = sock.recvfrom(256)
            rx = 1
            if address != (peer, port) or response != payload:
                raise ValueError("single-packet response mismatch")
            # Observe a bounded quiet interval.  No retransmission is allowed;
            # packet counts refer to this exchange, not unrelated link traffic.
            if select.select([sock], [], [], 0.2)[0]:
                raise ValueError("duplicate single-packet response")
        _record(
            console,
            nonce,
            tx=1,
            rx=1,
            tx_errors=0,
            rx_errors=0,
            payload_sha256=digest,
            status="pass",
        )
        return 0
    except (OSError, ValueError) as error:
        try:
            _record(
                console,
                nonce,
                tx=tx,
                rx=rx,
                tx_errors=int(tx == 0),
                rx_errors=1,
                payload_sha256=digest,
                status="fail",
            )
        except OSError:
            print(f"single-packet evidence: {error}", file=sys.stderr)
        return 1


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmdline", type=Path, default=Path("/proc/cmdline"))
    parser.add_argument("--console", type=Path, default=Path("/dev/console"))
    parser.add_argument("--timeout", type=float, default=15.0)
    values = parser.parse_args(arguments)
    return run(cmdline=values.cmdline, console=values.console, timeout=values.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
