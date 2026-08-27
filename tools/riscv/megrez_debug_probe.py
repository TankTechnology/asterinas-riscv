# SPDX-License-Identifier: MPL-2.0

"""Bounded HTTP responder shared by Megrez simulation and board gates."""

from __future__ import annotations

import socket
import threading
from types import TracebackType

PROBE_HOST = "10.100.19.216"
PROBE_PORT = 18080
MAX_REQUEST_BYTES = 64 * 1024
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

    def __init__(self, *, host: str = PROBE_HOST, port: int = PROBE_PORT) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("probe host must be a non-empty string")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise ValueError("probe port must be in [0, 65535]")
        self._host = host
        self._port = port
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

    @staticmethod
    def _valid_request(request: bytes) -> bool:
        first_line = request.split(b"\r\n", 1)[0]
        return first_line in (
            b"GET /asterinas-probe HTTP/1.0",
            b"GET /asterinas-probe HTTP/1.1",
        )

    def _handle(self, connection: socket.socket) -> None:
        request = bytearray()
        connection.settimeout(2.0)
        while b"\r\n\r\n" not in request and len(request) < MAX_REQUEST_BYTES:
            chunk = connection.recv(min(4096, MAX_REQUEST_BYTES - len(request)))
            if not chunk:
                break
            request.extend(chunk)
        response = (
            PROBE_RESPONSE
            if self._valid_request(bytes(request))
            else NOT_FOUND_RESPONSE
        )
        connection.sendall(response)

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
