#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Answer the guest's own resolver by tunnelling each query to the host.

The board can reach the host and nothing else: its boot arguments configure a
static neighbour for the host, so no resolver is reachable directly and
/etc/resolv.conf cannot name one. This binds the loopback address and port the
guest can always reach and carries each query to a host-side forwarder over the
one path that exists.

A query travels as the two-byte length prefix and question that RFC 1035
section 4.2.2 defines for DNS over TCP, so the host side has to be a resolver
that speaks DNS over TCP rather than a plain TCP-to-UDP relay.

resolv.conf is written only after a probe query has come back, so a shim whose
tunnel is down leaves the guest exactly as it found it instead of pointing the
resolver at a port nothing answers.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

LISTEN_ADDRESS = "127.0.0.1"
LISTEN_PORT = 53
DEFAULT_UPSTREAM_PORT = 15354
TIMEOUT = 5.0
MAX_DATAGRAM = 4096
RESOLV_CONF = "/etc/resolv.conf"

# One root NS query. Any working resolver answers it, so the probe does not
# depend on a particular name resolving and cannot be fooled by an upstream
# that happens to be filtering one.
_PROBE = (
    b"\x12\x34"  # transaction id
    b"\x01\x00"  # recursion desired
    b"\x00\x01"  # one question
    b"\x00\x00\x00\x00\x00\x00"
    b"\x00"  # root name
    b"\x00\x02"  # NS
    b"\x00\x01"  # IN
)


def upstream() -> tuple[str, int] | None:
    """Returns the forwarder to tunnel through, or None when unconfigured."""

    host = os.environ.get("ASTERINAS_DESKTOP_DNS_HOST", "").strip()
    raw_port = os.environ.get("ASTERINAS_DESKTOP_DNS_PORT", "").strip()
    if not host:
        return None
    if raw_port:
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            return None
        port = int(raw_port)
    else:
        port = DEFAULT_UPSTREAM_PORT
    return host, port


def resolve(query: bytes, target: tuple[str, int]) -> bytes:
    """Carries one query over a length-prefixed TCP stream to the forwarder."""

    with socket.create_connection(target, timeout=TIMEOUT) as stream:
        stream.sendall(len(query).to_bytes(2, "big") + query)
        header = b""
        while len(header) < 2:
            chunk = stream.recv(2 - len(header))
            if not chunk:
                raise OSError("forwarder closed before its length prefix")
            header += chunk
        length = int.from_bytes(header, "big")
        answer = b""
        while len(answer) < length:
            chunk = stream.recv(length - len(answer))
            if not chunk:
                raise OSError("forwarder closed mid-answer")
            answer += chunk
        return answer


def publish_resolver() -> None:
    """Points the guest resolver at this shim once it has proven usable."""

    line = f"nameserver {LISTEN_ADDRESS}\n"
    try:
        with open(RESOLV_CONF, "rb") as handle:
            if handle.read() == line.encode():
                return
    except OSError:
        pass
    temporary = f"{RESOLV_CONF}.dns-shim"
    with open(temporary, "w", encoding="ascii") as handle:
        handle.write(line)
    os.replace(temporary, RESOLV_CONF)


def probe(target: tuple[str, int]) -> bool:
    """Returns whether the tunnel carries a query and returns an answer."""

    try:
        answer = resolve(_PROBE, target)
    except OSError as error:
        print(f"dns shim probe failed: {error}", file=sys.stderr, flush=True)
        return False
    # A response echoes the transaction id it answers.
    return answer[:2] == _PROBE[:2]


def serve(server: socket.socket, target: tuple[str, int]) -> None:
    while True:
        try:
            query, peer = server.recvfrom(MAX_DATAGRAM)
        except OSError:
            continue
        try:
            answer = resolve(query, target)
        except OSError as error:
            print(f"dns shim query failed: {error}", file=sys.stderr, flush=True)
            continue
        try:
            server.sendto(answer, peer)
        except OSError:
            continue


# Exit statuses the unit distinguishes. Unconfigured must not look like a
# clean run: with a zero exit the unit reported "inactive (dead)" and the
# missing resolver was invisible on the board, which is exactly the state the
# shim exists to remove.
EXIT_UNCONFIGURED = 3
EXIT_TUNNEL_DOWN = 1


def main() -> int:
    target = upstream()
    if target is None:
        print(
            "dns shim unconfigured: ASTERINAS_DESKTOP_DNS_HOST is unset or invalid",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_UNCONFIGURED
    if not probe(target):
        # Leave resolv.conf alone: the guest keeps whatever it had, which is no
        # worse than a resolver pointing at a port with nothing behind it. The
        # unit retries this status, because a tunnel can come up after the boot.
        return EXIT_TUNNEL_DOWN

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_ADDRESS, LISTEN_PORT))
    publish_resolver()
    print(f"dns shim answering on {LISTEN_ADDRESS}:{LISTEN_PORT}", flush=True)
    for _ in range(4):
        threading.Thread(target=serve, args=(server, target), daemon=True).start()
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
