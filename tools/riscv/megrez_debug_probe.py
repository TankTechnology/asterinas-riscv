# SPDX-License-Identifier: MPL-2.0

"""Bounded HTTP responder shared by Megrez simulation and board gates."""

from __future__ import annotations

import socket
import threading
from types import TracebackType

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
PROBE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 23\r\nConnection: close\r\n\r\n" + PROBE_BODY
)
NOT_FOUND_RESPONSE = (
    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)


class ProbeServerError(RuntimeError):
    """One stable responder setup or lifecycle failure."""


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
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve,
            name="megrez-probe-server",
            daemon=False,
        )
        self._thread.start()
        return self

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

    def _handle(self, connection: socket.socket) -> None:
        request = bytearray()
        connection.settimeout(2.0)
        while b"\r\n\r\n" not in request and len(request) < MAX_REQUEST_BYTES:
            chunk = connection.recv(min(4096, MAX_REQUEST_BYTES - len(request)))
            if not chunk:
                break
            request.extend(chunk)
        valid, payload_bytes = self._requested_payload(bytes(request))
        if not valid:
            connection.sendall(NOT_FOUND_RESPONSE)
            return
        if payload_bytes is None:
            connection.sendall(PROBE_RESPONSE)
            return

        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(payload_bytes).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
        )
        offset = 0
        while offset < payload_bytes:
            amount = min(PROBE_CHUNK_BYTES, payload_bytes - offset)
            connection.sendall(bytes((offset + index) % 251 for index in range(amount)))
            offset += amount
        if self._payload_sizes is not None:
            self._next_payload += 1

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, _peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if not self._stop.is_set():
                    self._failure = error
                return
            try:
                with connection:
                    self._handle(connection)
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
        thread.join(timeout=2.0)
        self._listener = None
        self._thread = None
        if thread.is_alive():
            raise ProbeServerError("probe-server-stop-timeout")
        if self._failure is not None and exception_type is None:
            raise ProbeServerError(f"probe-server-runtime: {self._failure}")
