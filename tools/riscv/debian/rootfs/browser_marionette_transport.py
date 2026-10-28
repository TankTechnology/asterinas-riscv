#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Shared bounded Marionette transport for current browser checks."""

from __future__ import annotations

import errno
import json
import math
import os
import socket
import sys
import time
from collections.abc import Callable


MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_SECONDS = 10.0

class GateError(RuntimeError):
    """The browser did not provide exact, trustworthy content evidence."""


class CommandNotSentTimeout(TimeoutError):
    """The deadline expired before any command bytes could have been sent."""


class Marionette:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        phase: Callable[[str, str, BaseException | None], None] | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise GateError("Marionette endpoint must be loopback")
        self._diagnostics = os.environ.get("ASTERINAS_MARIONETTE_DIAGNOSTICS") == "1"
        self._request_id = 0
        self._pending_request: int | None = None
        self._timed_out = False
        self._command_name = "greeting"
        self._reset_progress()
        self._stage = "tcp_connect"
        self._deadline = time.monotonic() + timeout
        self._phase = phase or (lambda _phase, _state, _error=None: None)
        self._phase("tcp-connect", "start", None)
        try:
            self._socket = socket.create_connection((host, port), timeout=timeout)
        except BaseException as error:
            self._diagnostic("failure", error)
            self._phase("tcp-connect", "exception", error)
            raise
        self._phase("tcp-connect", "done", None)
        self._next_id = 1
        self._phase("greeting", "start", None)
        try:
            hello = self._receive()
            if hello != {"applicationType": "gecko", "marionetteProtocol": 3}:
                raise GateError("unexpected Marionette protocol greeting")
        except BaseException as error:
            self._diagnostic("failure", error)
            self._phase("greeting", "exception", error)
            self.close()
            raise
        self._phase("greeting", "done", None)

    def _reset_progress(self) -> None:
        self._send_complete = False
        self._header_bytes = 0
        self._body_expected: int | None = None
        self._body_received = 0
        # A timeout can occur in either field. Keep the consumed bytes until
        # this exact response is validated; another command must not replace it.
        self._frame_header = bytearray()
        self._frame_body = bytearray()

    def _diagnostic(self, event: str, error: BaseException | None = None) -> None:
        if not self._diagnostics:
            return
        # Four records per command at most, independent of frame size or TCP
        # fragmentation. Never include scripts, URLs, nonces or response data.
        record = {
            "version": 1,
            "event": event,
            "pid": os.getpid(),
            "monotonic_ns": time.monotonic_ns(),
            "request_id": self._request_id,
            "command": self._command_name,
            "stage": self._stage,
            "send_complete": self._send_complete,
            "header_bytes": self._header_bytes,
            "body_expected": self._body_expected,
            "body_received": self._body_received,
        }
        if error is not None:
            record["error_type"] = type(error).__name__
            record["errno"] = error.errno if isinstance(error, OSError) else None
        try:
            print(
                "A_WEB_MARIONETTE_TRANSPORT "
                + json.dumps(record, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            )
        except (OSError, ValueError):
            # A diagnostic sink failure must not replace the protocol error.
            pass

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Marionette gate deadline expired")
        return remaining

    def set_timeout(self, timeout: float) -> None:
        """Start a fresh bounded phase without inheriting setup time."""

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Marionette timeout must be finite and positive")
        self._deadline = time.monotonic() + timeout

    def _read_exact(self, length: int, *, header: bool = False) -> bytes:
        data = bytearray() if header else self._frame_body
        while len(data) < length:
            self._socket.settimeout(self._remaining())
            chunk = self._socket.recv(length - len(data))
            if not chunk:
                raise GateError("truncated Marionette message")
            data.extend(chunk)
            if header:
                self._header_bytes += len(chunk)
            else:
                self._body_received += len(chunk)
        return bytes(data)

    def _receive(self) -> object:
        if self._body_expected is None:
            self._stage = "response_header"
            while True:
                byte = self._read_exact(1, header=True)
                if byte == b":":
                    break
                if not byte.isdigit() or len(self._frame_header) >= 10:
                    raise GateError("invalid Marionette frame length")
                self._frame_header.extend(byte)
            if not self._frame_header:
                raise GateError("missing Marionette frame length")
            length = int(self._frame_header)
            if length > MAX_MESSAGE_BYTES:
                raise GateError("oversized Marionette message")
            self._body_expected = length
            self._stage = "response_body"
            self._diagnostic("frame_header")
        self._stage = "response_body"
        payload = self._read_exact(self._body_expected)
        self._stage = "response_json"
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateError("invalid Marionette JSON") from error

    def command(self, name: str, parameters: object | None = None) -> object:
        if self._pending_request is not None:
            raise GateError("unresolved Marionette response prevents another command")
        try:
            self._remaining()
        except TimeoutError as error:
            raise CommandNotSentTimeout(str(error)) from error
        identifier = self._next_id
        self._next_id += 1
        self._request_id = identifier
        self._command_name = name
        self._reset_progress()
        self._timed_out = False
        self._stage = "send"
        self._diagnostic("begin")
        try:
            result = self._command(identifier, name, parameters)
        except BaseException as error:
            self._timed_out = (
                isinstance(error, TimeoutError) and self._pending_request is not None
            )
            self._diagnostic("failure", error)
            if isinstance(error, TimeoutError) and self._pending_request is None:
                raise CommandNotSentTimeout(str(error)) from error
            raise
        self._stage = "complete"
        self._diagnostic("complete")
        return result

    def _command(self, identifier: int, name: str, parameters: object | None) -> object:
        payload = json.dumps(
            [0, identifier, name, parameters or {}], separators=(",", ":")
        ).encode()
        self._socket.settimeout(self._remaining())
        # From this point a failed sendall may have transmitted a partial frame.
        # Earlier preparation/deadline failures cannot poison response framing.
        self._pending_request = identifier
        self._socket.sendall(str(len(payload)).encode("ascii") + b":" + payload)
        self._send_complete = True
        self._diagnostic("send_complete")
        response = self._receive()
        error, result = self._response_fields(response, identifier)
        if error is not None:
            # Keep the public failure contract stable, but expose the exact
            # Marionette error when an investigator explicitly opts in.  This
            # is essential for separating a missing browsing context from a
            # JavaScript/runtime failure without making normal gate logs noisy.
            if os.environ.get("ASTERINAS_MARIONETTE_DEBUG_ERRORS") == "1":
                try:
                    detail = json.dumps(error, sort_keys=True, separators=(",", ":"))
                except (TypeError, ValueError):
                    detail = repr(error)
                print(
                    f"A_WEB_MARIONETTE_ERROR command={name} error={detail}",
                    file=sys.stderr,
                    flush=True,
                )
            raise GateError(f"Marionette command failed: {name}")
        return result

    def _response_fields(
        self, response: object, identifier: int
    ) -> tuple[object, object]:
        self._stage = "response_identity"
        if not isinstance(response, list) or len(response) != 4:
            raise GateError("malformed Marionette response")
        kind, response_id, error, result = response
        if (
            type(kind) is not int
            or type(response_id) is not int
            or kind != 1
            or response_id != identifier
        ):
            raise GateError("unexpected Marionette response identity")
        self._pending_request = None
        return error, result

    def recover_timed_out_command(self, timeout: float) -> object:
        """Drain only the fully sent request that timed out, without sending bytes.

        Partial response framing survives the timeout. An incomplete send or a
        mismatched response leaves the connection unusable for further commands.
        The original operation remains failed even if its late reply succeeds.
        """
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_RECOVERY_SECONDS
        ):
            raise ValueError("Marionette recovery timeout is outside its bound")
        if (
            not self._timed_out
            or not self._send_complete
            or self._pending_request is None
        ):
            raise GateError("no recoverable timed-out Marionette command")
        self.set_timeout(timeout)
        self._timed_out = False
        try:
            response = self._receive()
        except TimeoutError:
            self._timed_out = True
            raise
        # A remote command error still completes the outstanding response. The
        # caller already has the original timeout and needs only protocol safety.
        _error, result = self._response_fields(response, self._pending_request)
        return result

    def close(self) -> None:
        self._socket.close()


def _connect(
    host: str,
    port: int,
    deadline: float,
    phase: Callable[[str, str, BaseException | None], None] | None = None,
) -> Marionette:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("Marionette endpoint did not become ready before deadline")
        try:
            if phase is None:
                return Marionette(host, port, remaining)
            return Marionette(host, port, remaining, phase=phase)
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError(
                    "Marionette endpoint did not become ready before deadline"
                ) from error
            time.sleep(min(0.1, remaining))
