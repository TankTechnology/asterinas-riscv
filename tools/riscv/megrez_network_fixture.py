#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Serve one deterministic payload for bounded Megrez network gates."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import signal
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


FIXTURE_PATH = "/asterinas-network-probe.bin"
PAYLOAD_SIZE = 64 * 1024
PAYLOAD = bytes(range(256)) * (PAYLOAD_SIZE // 256)
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
MAX_REQUEST_RECORDS = 64


def is_successful_summary(
    summary: Mapping[str, object], *, expected_requests: int
) -> bool:
    """Require an exact set of successful fixed-payload requests."""

    if (
        isinstance(expected_requests, bool)
        or not isinstance(expected_requests, int)
        or not 0 < expected_requests <= MAX_REQUEST_RECORDS
    ):
        return False
    requests = summary.get("requests")
    if not isinstance(requests, list) or len(requests) != expected_requests:
        return False
    if (
        summary.get("schema_version") != 1
        or summary.get("payload_path") != FIXTURE_PATH
        or summary.get("payload_sha256") != PAYLOAD_SHA256
        or summary.get("payload_size") != PAYLOAD_SIZE
        or summary.get("request_count") != expected_requests
        or summary.get("records_truncated") is not False
    ):
        return False
    return all(
        isinstance(record, dict)
        and record.get("body_bytes") == PAYLOAD_SIZE
        and record.get("path") == FIXTURE_PATH
        and record.get("status") == 200
        for record in requests
    )


@dataclass(frozen=True)
class FixtureConfig:
    """The exact local listener and optional peer restriction."""

    bind_address: str = "127.0.0.1"
    port: int = 17894
    allowed_peer: str | None = None

    def __post_init__(self) -> None:
        _validate_ipv4(self.bind_address, "bind address")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("port must be an integer between 0 and 65535")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        if self.allowed_peer is not None:
            _validate_ipv4(self.allowed_peer, "allowed peer")


def _validate_ipv4(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an IPv4 address")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an IPv4 address") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical IPv4 address")


class FixtureServer:
    """One explicitly owned ThreadingHTTPServer with bounded evidence."""

    def __init__(self, config: FixtureConfig) -> None:
        self.config = config
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._request_count = 0
        self._records: list[dict[str, object]] = []
        self._last_timestamp = 0

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self.close()

    @property
    def address(self) -> str:
        server = self._require_server()
        return str(server.server_address[0])

    @property
    def port(self) -> int:
        server = self._require_server()
        return int(server.server_address[1])

    @property
    def endpoint(self) -> str:
        return f"http://{self.address}:{self.port}{FIXTURE_PATH}"

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None

    @property
    def thread(self) -> threading.Thread:
        if self._thread is None:
            raise RuntimeError("fixture server is not running")
        return self._thread

    def _require_server(self) -> http.server.ThreadingHTTPServer:
        if self._server is None:
            raise RuntimeError("fixture server is not running")
        return self._server

    def start(self) -> FixtureServer:
        if self._server is not None:
            return self
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle_get(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(
            (self.config.bind_address, self.config.port), Handler
        )
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="megrez-network-fixture",
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                raise RuntimeError("fixture server thread did not stop")

    def _handle_get(self, request: http.server.BaseHTTPRequestHandler) -> None:
        peer = request.client_address[0]
        if self.config.allowed_peer is not None and peer != self.config.allowed_peer:
            status = 403
            body = b""
        elif request.path != FIXTURE_PATH:
            status = 404
            body = b""
        else:
            status = 200
            body = PAYLOAD

        request.send_response(status)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Content-Type", "application/octet-stream")
        request.send_header("Cache-Control", "no-store")
        request.send_header("Connection", "close")
        request.end_headers()
        try:
            request.wfile.write(body)
        finally:
            self._record(peer, request.path, status, len(body))

    def _record(self, peer: str, path: str, status: int, body_bytes: int) -> None:
        with self._lock:
            self._request_count += 1
            now = max(time.monotonic_ns(), self._last_timestamp + 1)
            self._last_timestamp = now
            if len(self._records) < MAX_REQUEST_RECORDS:
                self._records.append(
                    {
                        "body_bytes": body_bytes,
                        "monotonic_ns": now,
                        "path": path,
                        "peer": peer,
                        "status": status,
                    }
                )

    def summary(self) -> dict[str, object]:
        """Return a detached canonical-schema snapshot of bounded evidence."""

        with self._lock:
            records = [dict(record) for record in self._records]
            request_count = self._request_count
        return {
            "payload_path": FIXTURE_PATH,
            "payload_sha256": PAYLOAD_SHA256,
            "payload_size": PAYLOAD_SIZE,
            "records_truncated": request_count > len(records),
            "request_count": request_count,
            "requests": records,
            "schema_version": 1,
        }

    def summary_json(self) -> bytes:
        return (
            json.dumps(self.summary(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


def _ipv4_argument(value: str) -> str:
    try:
        _validate_ipv4(value, "address")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _parse_args(arguments: Sequence[str] | None = None) -> FixtureConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-address", type=_ipv4_argument, default="127.0.0.1")
    parser.add_argument("--port", type=_port_argument, default=17894)
    parser.add_argument("--allow-peer", type=_ipv4_argument)
    values = parser.parse_args(arguments)
    return FixtureConfig(values.bind_address, values.port, values.allow_peer)


def main(arguments: Sequence[str] | None = None) -> int:
    config = _parse_args(arguments)
    stop = threading.Event()
    previous: dict[int, signal.Handlers] = {}

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        with FixtureServer(config) as server:
            print(server.endpoint, flush=True)
            stop.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
