# SPDX-License-Identifier: MPL-2.0

"""Bounded HTTP responder shared by Megrez simulation and board gates."""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from types import TracebackType
from typing import Any

PROBE_HOST = "10.100.19.216"
PROBE_PORT = 18080
MAX_REQUEST_BYTES = 64 * 1024
PROBE_STRESS_BYTES = 16 * 1024 * 1024
PROBE_STRESS_SIZES = (
    16 * 1024,
    64 * 1024,
    1024 * 1024,
    PROBE_STRESS_BYTES,
)
MAX_PROBE_PAYLOAD_BYTES = 64 * 1024 * 1024
PROBE_CHUNK_BYTES = 64 * 1024
PROBE_BODY = b"ASTERINAS_TCP_PROBE_OK\n"
TCP_INFO_SAMPLE_LIMIT = 4096
TCP_INFO_SAMPLE_INTERVAL = 0.02
TCP_INFO_POST_SEND_SECONDS = 0.25
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PROBE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 23\r\nConnection: close\r\n\r\n" + PROBE_BODY
)
NOT_FOUND_RESPONSE = (
    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)


class ProbeServerError(RuntimeError):
    """One stable responder setup or lifecycle failure."""


def _tcp_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], sys.byteorder)


def _tcp_u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 8], sys.byteorder)


def _tcp_info_sample(connection: socket.socket, started_ns: int) -> dict[str, int]:
    data = connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
    if len(data) < 232:
        raise OSError("TCP_INFO response is shorter than 232 bytes")
    return {
        "monotonic_us": (time.monotonic_ns() - started_ns) // 1000,
        "state": data[0],
        "retransmits": data[2],
        "rto_us": _tcp_u32(data, 8),
        "snd_mss": _tcp_u32(data, 16),
        "unacked": _tcp_u32(data, 24),
        "lost": _tcp_u32(data, 32),
        "retrans": _tcp_u32(data, 36),
        "snd_cwnd": _tcp_u32(data, 80),
        "total_retrans": _tcp_u32(data, 100),
        "bytes_acked": _tcp_u64(data, 120),
        "segs_out": _tcp_u32(data, 136),
        "segs_in": _tcp_u32(data, 140),
        "data_segs_out": _tcp_u32(data, 156),
        "bytes_sent": _tcp_u64(data, 200),
        "bytes_retrans": _tcp_u64(data, 208),
        "snd_wnd": _tcp_u32(data, 228),
    }


class _ConnectionTrace:
    def __init__(self, peer: tuple[Any, ...]) -> None:
        self._lock = threading.Lock()
        self._started_ns = time.monotonic_ns()
        self._peer = str(peer[0]) if peer else "unknown"
        self._requested_bytes: int | None = None
        self._application_bytes_accepted = 0
        self._last_application_send_us = 0
        self._payload_bytes_accepted = 0
        self._outcome = "in-progress"
        self._sampling_error: str | None = None
        self._samples: list[dict[str, int]] = []
        self._samples_dropped = 0

    @property
    def started_ns(self) -> int:
        return self._started_ns

    def set_requested_bytes(self, requested_bytes: int | None) -> None:
        with self._lock:
            self._requested_bytes = requested_bytes

    def record_send(self, amount: int, *, payload: bool) -> None:
        with self._lock:
            self._application_bytes_accepted += amount
            self._last_application_send_us = (
                time.monotonic_ns() - self._started_ns
            ) // 1000
            if payload:
                self._payload_bytes_accepted += amount

    def add_sample(self, sample: dict[str, int]) -> None:
        with self._lock:
            if len(self._samples) == TCP_INFO_SAMPLE_LIMIT:
                self._samples_dropped += 1
                return
            self._samples.append(sample)

    def set_sampling_error(self, error: OSError) -> None:
        with self._lock:
            if self._sampling_error is None:
                self._sampling_error = type(error).__name__

    def finish(self, outcome: str) -> None:
        with self._lock:
            self._outcome = outcome

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "peer": self._peer,
                "requested_bytes": self._requested_bytes,
                "application_bytes_accepted": self._application_bytes_accepted,
                "last_application_send_us": self._last_application_send_us,
                "payload_bytes_accepted": self._payload_bytes_accepted,
                "outcome": self._outcome,
                "sampling_error": self._sampling_error,
                "samples_dropped": self._samples_dropped,
                "samples": [dict(sample) for sample in self._samples],
            }


class ProbeServer:
    """Serve the fixed Megrez TCP-probe response for one gate lifecycle."""

    def __init__(
        self,
        *,
        host: str = PROBE_HOST,
        port: int = PROBE_PORT,
        payload_bytes: int | None = PROBE_STRESS_BYTES,
        payload_sizes: tuple[int, ...] | None = None,
    ) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("probe host must be a non-empty string")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise ValueError("probe port must be in [0, 65535]")
        if payload_bytes is not None and (
            isinstance(payload_bytes, bool)
            or not isinstance(payload_bytes, int)
            or not 0 < payload_bytes <= MAX_PROBE_PAYLOAD_BYTES
        ):
            raise ValueError("probe payload bytes must be in [1, 64 MiB]")
        if payload_sizes is not None and (
            not payload_sizes
            or any(
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 < size <= MAX_PROBE_PAYLOAD_BYTES
                for size in payload_sizes
            )
        ):
            raise ValueError("probe payload sizes must be in [1, 64 MiB]")
        self._host = host
        self._port = port
        self._payload_bytes = payload_bytes
        self._payload_sizes = payload_sizes
        self._next_payload = 0
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._failure: OSError | None = None
        self._trace_lock = threading.Lock()
        self._traces: list[_ConnectionTrace] = []
        self._active_connections: set[socket.socket] = set()

    @property
    def address(self) -> tuple[str, int]:
        """Return the bound IPv4 address after entering the context."""

        if self._listener is None:
            raise ProbeServerError("probe-server-not-running")
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    def __enter__(self) -> ProbeServer:
        if self._listener is not None:
            raise ProbeServerError("probe-server-already-running")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._port))
            listener.listen(8)
            listener.settimeout(0.1)
        except OSError as error:
            listener.close()
            raise ProbeServerError(f"probe-server-bind: {error}") from error
        self._stop.clear()
        self._failure = None
        with self._trace_lock:
            self._traces.clear()
            self._active_connections.clear()
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve,
            name="megrez-probe-server",
            daemon=False,
        )
        self._thread.start()
        return self

    def trace_snapshot(self, *, plan_sha256: str) -> dict[str, object]:
        """Return one bounded trace bound to an exact physical plan."""

        if _SHA256_PATTERN.fullmatch(plan_sha256) is None:
            raise ValueError("plan SHA-256 must be 64 lowercase hexadecimal digits")
        with self._trace_lock:
            traces = tuple(self._traces)
        return {
            "schema_version": 1,
            "plan_sha256": plan_sha256,
            "sample_interval_ms": int(TCP_INFO_SAMPLE_INTERVAL * 1000),
            "sample_limit": TCP_INFO_SAMPLE_LIMIT,
            "post_send_observation_ms": int(TCP_INFO_POST_SEND_SECONDS * 1000),
            "connections": [trace.snapshot() for trace in traces],
        }

    def canonical_trace(self, *, plan_sha256: str) -> bytes:
        """Encode the current bounded trace as canonical UTF-8 JSON."""

        return (
            json.dumps(
                self.trace_snapshot(plan_sha256=plan_sha256),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    def _requested_payload(self, request: bytes) -> tuple[bool, int | None]:
        first_line = request.split(b"\r\n", 1)[0]
        if self._payload_sizes is None:
            if first_line in (
                b"GET /asterinas-probe HTTP/1.0",
                b"GET /asterinas-probe HTTP/1.1",
            ):
                return True, self._payload_bytes
            return False, None
        if self._next_payload >= len(self._payload_sizes):
            return False, None
        expected = self._payload_sizes[self._next_payload]
        if first_line not in (
            f"GET /asterinas-probe/{expected} HTTP/1.0".encode("ascii"),
            f"GET /asterinas-probe/{expected} HTTP/1.1".encode("ascii"),
        ):
            return False, None
        return True, expected

    @staticmethod
    def _send_all(
        connection: socket.socket,
        data: bytes,
        trace: _ConnectionTrace,
        *,
        payload: bool,
    ) -> None:
        view = memoryview(data)
        while view:
            amount = connection.send(view)
            if amount == 0:
                raise ConnectionError("probe socket closed during send")
            trace.record_send(amount, payload=payload)
            view = view[amount:]

    @staticmethod
    def _sample_connection(
        connection: socket.socket,
        trace: _ConnectionTrace,
        stop: threading.Event,
    ) -> None:
        while not stop.is_set():
            try:
                trace.add_sample(_tcp_info_sample(connection, trace.started_ns))
            except OSError as error:
                trace.set_sampling_error(error)
                return
            stop.wait(TCP_INFO_SAMPLE_INTERVAL)

    def _handle(
        self,
        connection: socket.socket,
        peer: tuple[Any, ...],
    ) -> None:
        trace = _ConnectionTrace(peer)
        with self._trace_lock:
            self._traces.append(trace)
            self._active_connections.add(connection)
        sample_stop = threading.Event()
        sampler = threading.Thread(
            target=self._sample_connection,
            args=(connection, trace, sample_stop),
            name="megrez-probe-tcp-info",
            daemon=False,
        )
        request = bytearray()
        connection.settimeout(2.0)
        sampler.start()
        try:
            while b"\r\n\r\n" not in request and len(request) < MAX_REQUEST_BYTES:
                chunk = connection.recv(min(4096, MAX_REQUEST_BYTES - len(request)))
                if not chunk:
                    break
                request.extend(chunk)
            valid, payload_bytes = self._requested_payload(bytes(request))
            trace.set_requested_bytes(payload_bytes)
            if not valid:
                self._send_all(connection, NOT_FOUND_RESPONSE, trace, payload=False)
                trace.finish("not-found")
                return
            if payload_bytes is None:
                self._send_all(connection, PROBE_RESPONSE, trace, payload=False)
                trace.finish("complete")
                return

            self._send_all(
                connection,
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(payload_bytes).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n",
                trace,
                payload=False,
            )
            offset = 0
            while offset < payload_bytes:
                amount = min(PROBE_CHUNK_BYTES, payload_bytes - offset)
                self._send_all(
                    connection,
                    bytes((offset + index) % 251 for index in range(amount)),
                    trace,
                    payload=True,
                )
                offset += amount
            self._stop.wait(TCP_INFO_POST_SEND_SECONDS)
            if self._payload_sizes is not None:
                self._next_payload += 1
            trace.finish("complete")
        except (OSError, TimeoutError) as error:
            trace.finish(f"socket-error:{type(error).__name__}")
            raise
        finally:
            sample_stop.set()
            sampler.join(timeout=0.5)
            if sampler.is_alive():
                trace.finish("sampler-stop-timeout")
            with self._trace_lock:
                self._active_connections.discard(connection)

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if not self._stop.is_set():
                    self._failure = error
                return
            try:
                with connection:
                    self._handle(connection, peer)
            except (OSError, TimeoutError):
                continue

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        listener = self._listener
        thread = self._thread
        if listener is None or thread is None:
            return
        self._stop.set()
        listener.close()
        with self._trace_lock:
            active = tuple(self._active_connections)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        thread.join(timeout=2.0)
        self._listener = None
        self._thread = None
        if thread.is_alive():
            raise ProbeServerError("probe-server-stop-timeout")
        if self._failure is not None and exception_type is None:
            raise ProbeServerError(f"probe-server-runtime: {self._failure}")
