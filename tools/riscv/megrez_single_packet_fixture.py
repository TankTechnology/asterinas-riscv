# SPDX-License-Identifier: MPL-2.0

"""One-shot UDP echo fixture for the Megrez single-packet gate.

The fixture counts application datagrams only.  Link setup, ARP, and other
control traffic never enters the contract because the socket is bound after
the gate has selected its fixed endpoint and validates one nonce-bound payload.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import selectors
import socket
import time
import threading
from typing import Any


PAYLOAD = b"ASTERINAS-SINGLE-UDP-V1"
MAX_DATAGRAM = 256


class SinglePacketFixtureError(ValueError):
    """The one-shot fixture observed an invalid application datagram."""


@dataclass(frozen=True)
class PacketSummary:
    request_count: int
    response_count: int
    payload_size: int
    payload_sha256: str
    peer: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "response_count": self.response_count,
            "payload_size": self.payload_size,
            "payload_sha256": self.payload_sha256,
            "peer": self.peer,
            "status": self.status,
        }


def nonce_payload(nonce: str) -> bytes:
    if not isinstance(nonce, str) or len(nonce) != 32 or any(
        character not in "0123456789abcdef" for character in nonce
    ):
        raise SinglePacketFixtureError("nonce must be 32 lowercase hexadecimal digits")
    return nonce.encode("ascii") + b":" + PAYLOAD


def expected_summary(nonce: str) -> PacketSummary:
    payload = nonce_payload(nonce)
    return PacketSummary(
        request_count=1,
        response_count=1,
        payload_size=len(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        peer="",
        status="pass",
    )


class OneShotUdpFixture:
    """Receive and echo exactly one nonce-bound UDP datagram."""

    def __init__(self, host: str, port: int, *, allowed_peer: str | None = None):
        if not host or not 0 <= port <= 65535:
            raise ValueError("invalid fixture endpoint")
        self.host = host
        self.port = port
        self.allowed_peer = allowed_peer
        self._socket: socket.socket | None = None
        self._cancel = threading.Event()

    def __enter__(self) -> "OneShotUdpFixture":
        if self._socket is not None:
            raise RuntimeError("fixture is already open")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        sock.setblocking(False)
        self._socket = sock
        self._cancel.clear()
        return self

    def cancel(self) -> None:
        """Interrupt a pending receive without closing its selected socket."""
        self._cancel.set()

    def __exit__(self, *_: object) -> None:
        if self._socket is not None:
            self._socket.close()
        self._socket = None

    def receive_once(self, nonce: str, timeout: float = 10.0) -> PacketSummary:
        if self._socket is None:
            raise RuntimeError("fixture is not open")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        expected = nonce_payload(nonce)
        selector = selectors.DefaultSelector()
        selector.register(self._socket, selectors.EVENT_READ)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if self._cancel.is_set():
                    raise SinglePacketFixtureError("single-packet receive cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SinglePacketFixtureError("single-packet receive timeout")
                if selector.select(min(remaining, 0.1)):
                    break
            payload, address = self._socket.recvfrom(MAX_DATAGRAM)
            peer, _ = address
            if self.allowed_peer is not None and peer != self.allowed_peer:
                raise SinglePacketFixtureError("unexpected single-packet peer")
            if payload != expected:
                raise SinglePacketFixtureError("single-packet payload mismatch")
            if self._socket.sendto(payload, address) != len(payload):
                raise SinglePacketFixtureError("short single-packet reply")
            # A bounded quiet interval catches delayed application retries.
            if selector.select(0.2):
                raise SinglePacketFixtureError("duplicate single-packet request")
            return PacketSummary(
                request_count=1,
                response_count=1,
                payload_size=len(payload),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                peer=peer,
                status="pass",
            )
        finally:
            selector.close()


def write_summary(path: str, summary: PacketSummary) -> None:
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(summary.as_dict(), stream, sort_keys=True)
        stream.write("\n")
