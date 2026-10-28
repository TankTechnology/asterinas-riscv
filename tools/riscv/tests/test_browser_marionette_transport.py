#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tools.riscv.debian.rootfs import browser_marionette_transport as gate


class _Client:
    responses: list[object] = []
    instance: "_Client | None" = None

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        phase: object | None = None,
    ) -> None:
        self.connection = (host, port, timeout)
        self.commands: list[tuple[str, object | None]] = []
        self.closed = False
        type(self).instance = self
        if phase is not None:
            phase("tcp-connect", "start", None)
            phase("tcp-connect", "done", None)
            phase("greeting", "start", None)
            phase("greeting", "done", None)

    def command(self, name: str, parameters: object | None = None) -> object:
        self.commands.append((name, parameters))
        return type(self).responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _frame(value: object) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode()
    return str(len(payload)).encode() + b":" + payload


class _Socket:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        if timeout <= 0:
            raise AssertionError("non-positive timeout")

    def recv(self, length: int) -> bytes:
        if not self.incoming:
            raise socket.timeout("mock deadline")
        result = bytes(self.incoming[:length])
        del self.incoming[:length]
        return result

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def close(self) -> None:
        self.closed = True


class BrowserMarionetteTransportTests(unittest.TestCase):
    def test_protocol_requires_v3_greeting_and_exact_response_id(self) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
            + _frame([1, 1, None, {"value": "ok"}])
        )
        phases = []
        with mock.patch.object(gate.socket, "create_connection", return_value=transport):
            client = gate.Marionette(
                "127.0.0.1", 2828, 5,
                phase=lambda name, state, error=None: phases.append(
                    (name, state, type(error).__name__ if error else None)
                ),
            )
        self.assertEqual(
            phases,
            [
                ("tcp-connect", "start", None),
                ("tcp-connect", "done", None),
                ("greeting", "start", None),
                ("greeting", "done", None),
            ],
        )
        self.assertEqual(client.command("WebDriver:Test"), {"value": "ok"})
        length, payload = bytes(transport.sent).split(b":", 1)
        self.assertEqual(int(length), len(payload))
        self.assertEqual(json.loads(payload), [0, 1, "WebDriver:Test", {}])
        client.close()
        self.assertTrue(transport.closed)

        bad = _Socket(_frame({"applicationType": "gecko", "marionetteProtocol": 2}))
        with (
            mock.patch.object(gate.socket, "create_connection", return_value=bad),
            self.assertRaisesRegex(gate.GateError, "greeting"),
        ):
            gate.Marionette("127.0.0.1", 2828, 5)
        self.assertTrue(bad.closed)

    def test_protocol_can_emit_opt_in_raw_command_errors(self) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
            + _frame([
                1,
                1,
                {"error": "no such window", "message": "Browsing context discarded"},
                None,
            ])
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(gate.socket, "create_connection", return_value=transport),
            mock.patch.dict(os.environ, {"ASTERINAS_MARIONETTE_DEBUG_ERRORS": "1"}),
            redirect_stderr(stderr),
        ):
            client = gate.Marionette("127.0.0.1", 2828, 5)
            with self.assertRaisesRegex(gate.GateError, "WebDriver:ExecuteScript"):
                client.command("WebDriver:ExecuteScript")
            client.close()
        diagnostic = stderr.getvalue()
        self.assertIn("A_WEB_MARIONETTE_ERROR", diagnostic)
        self.assertIn("no such window", diagnostic)
        self.assertIn("Browsing context discarded", diagnostic)

    def test_recovery_retains_partial_frames_and_sends_no_new_request(self) -> None:
        response = _frame([1, 1, None, {"value": "late"}])
        for cut in (1, response.index(b":") + 6):
            with self.subTest(cut=cut):
                transport = _Socket(
                    _frame({"applicationType": "gecko", "marionetteProtocol": 3})
                    + response[:cut]
                )
                with mock.patch.object(
                    gate.socket, "create_connection", return_value=transport
                ):
                    client = gate.Marionette("127.0.0.1", 2828, 1)
                with self.assertRaises(TimeoutError):
                    client.command("WebDriver:Navigate")
                sent = bytes(transport.sent)
                transport.incoming.extend(response[cut:])
                self.assertTrue(
                    callable(getattr(client, "recover_timed_out_command", None))
                )
                self.assertEqual(client.recover_timed_out_command(1), {"value": "late"})
                self.assertEqual(bytes(transport.sent), sent)
                transport.incoming.extend(_frame([1, 2, None, {"value": "next"}]))
                self.assertEqual(
                    client.command("WebDriver:GetWindowHandle"), {"value": "next"}
                )
                client.close()

    def test_timeout_requires_recovery_before_another_request(self) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
        )
        with mock.patch.object(
            gate.socket, "create_connection", return_value=transport
        ):
            client = gate.Marionette("127.0.0.1", 2828, 1)
        with self.assertRaises(TimeoutError):
            client.command("WebDriver:Navigate")
        sent = bytes(transport.sent)
        client.set_timeout(1)
        with self.assertRaisesRegex(gate.GateError, "unresolved"):
            client.command("WebDriver:GetWindowHandles")
        self.assertEqual(bytes(transport.sent), sent)

    def test_expired_before_send_does_not_poison_the_cleanup_command(self) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
        )
        now = [1.0]
        with (
            mock.patch.object(gate.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(gate.socket, "create_connection", return_value=transport),
        ):
            client = gate.Marionette("127.0.0.1", 2828, 1)
            now[0] = 3.0
            with self.assertRaises(TimeoutError):
                client.command("WebDriver:NewWindow")
            self.assertEqual(bytes(transport.sent), b"")
            with self.assertRaises(gate.GateError):
                client.recover_timed_out_command(1)
            client.set_timeout(1)
            transport.incoming.extend(_frame([1, 1, None, {"value": ["original"]}]))
            self.assertEqual(
                client.command("WebDriver:GetWindowHandles"), {"value": ["original"]}
            )
            self.assertNotIn(b"WebDriver:NewWindow", transport.sent)

    def test_expiry_while_preparing_a_request_still_counts_as_never_sent(self) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
        )
        now = [1.0]
        original_dumps = json.dumps

        def expire(value, **kwargs):
            now[0] = 3.0
            return original_dumps(value, **kwargs)

        with (
            mock.patch.object(gate.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(gate.socket, "create_connection", return_value=transport),
        ):
            client = gate.Marionette("127.0.0.1", 2828, 1)
            with (
                mock.patch.object(gate.json, "dumps", side_effect=expire),
                self.assertRaises(TimeoutError),
            ):
                client.command("WebDriver:NewWindow")
            self.assertEqual(bytes(transport.sent), b"")
            with self.assertRaises(gate.GateError):
                client.recover_timed_out_command(1)
            client.set_timeout(1)
            transport.incoming.extend(_frame([1, 2, None, {}]))
            self.assertEqual(client.command("WebDriver:GetWindowHandles"), {})

    def test_recovery_rejects_wrong_response_identity_and_incomplete_send(self) -> None:
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                transport = _Socket(
                    _frame({"applicationType": "gecko", "marionetteProtocol": 3})
                )
                with mock.patch.object(
                    gate.socket, "create_connection", return_value=transport
                ):
                    client = gate.Marionette("127.0.0.1", 2828, 1)
                if incomplete:

                    def partial_send(value):
                        transport.sent.extend(value[:3])
                        raise TimeoutError("partial send")

                    transport.sendall = partial_send
                with self.assertRaises(TimeoutError):
                    client.command("WebDriver:NewWindow")
                sent = bytes(transport.sent)
                transport.incoming.extend(
                    _frame([1, 2, None, {"value": "wrong request"}])
                )
                with self.assertRaises(gate.GateError):
                    client.recover_timed_out_command(1)
                with self.assertRaises(gate.GateError):
                    client.command("WebDriver:GetWindowHandles")
                self.assertEqual(bytes(transport.sent), sent)

    def test_recovery_is_bounded_and_requires_an_outstanding_timeout(self) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
        )
        with mock.patch.object(
            gate.socket, "create_connection", return_value=transport
        ):
            client = gate.Marionette("127.0.0.1", 2828, 1)
        for timeout in (0, -1, float("nan"), True, 11):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                client.recover_timed_out_command(timeout)
        with self.assertRaises(gate.GateError):
            client.recover_timed_out_command(1)
        self.assertEqual(bytes(transport.sent), b"")

    def test_recovery_rejects_noninteger_response_identity(self) -> None:
        for kind, identifier in ((True, 1), (1, True), (1.0, 1), (1, 1.0)):
            with self.subTest(kind=kind, identifier=identifier):
                transport = _Socket(
                    _frame({"applicationType": "gecko", "marionetteProtocol": 3})
                )
                with mock.patch.object(
                    gate.socket, "create_connection", return_value=transport
                ):
                    client = gate.Marionette("127.0.0.1", 2828, 1)
                with self.assertRaises(TimeoutError):
                    client.command("WebDriver:Navigate")
                transport.incoming.extend(_frame([kind, identifier, None, {}]))
                with self.assertRaises(gate.GateError):
                    client.recover_timed_out_command(1)

    def test_malformed_recovery_cannot_resume_at_a_different_frame_boundary(
        self,
    ) -> None:
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
        )
        with mock.patch.object(
            gate.socket, "create_connection", return_value=transport
        ):
            client = gate.Marionette("127.0.0.1", 2828, 1)
        with self.assertRaises(TimeoutError):
            client.command("WebDriver:Navigate")
        transport.incoming.extend(b"x")
        with self.assertRaises(gate.GateError):
            client.recover_timed_out_command(1)
        transport.incoming.extend(_frame([1, 1, None, {}]))
        with self.assertRaises(gate.GateError):
            client.recover_timed_out_command(1)

    def test_recovery_timeout_can_resume_without_forgetting_more_partial_bytes(
        self,
    ) -> None:
        response = _frame([1, 1, None, {}])
        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3}) + response[:1]
        )
        with mock.patch.object(
            gate.socket, "create_connection", return_value=transport
        ):
            client = gate.Marionette("127.0.0.1", 2828, 1)
        with self.assertRaises(TimeoutError):
            client.command("WebDriver:Navigate")
        transport.incoming.extend(response[1:6])
        with self.assertRaises(TimeoutError):
            client.recover_timed_out_command(1)
        transport.incoming.extend(response[6:])
        self.assertEqual(client.recover_timed_out_command(1), {})

    def test_protocol_rejects_non_loopback_and_timeout(self) -> None:
        with self.assertRaisesRegex(gate.GateError, "loopback"):
            gate.Marionette("192.0.2.1", 2828, 5)
        timed_out = _Socket(b"")
        with (
            mock.patch.object(gate.socket, "create_connection", return_value=timed_out),
            self.assertRaises(socket.timeout),
        ):
            gate.Marionette("127.0.0.1", 2828, 0.1)
        self.assertTrue(timed_out.closed)

    def test_protocol_frame_limit_allows_online_screenshots_but_stays_bounded(self) -> None:
        self.assertEqual(gate.MAX_MESSAGE_BYTES, 16 * 1024 * 1024)
        oversized = _Socket(f"{gate.MAX_MESSAGE_BYTES + 1}:".encode())
        with (
            mock.patch.object(gate.socket, "create_connection", return_value=oversized),
            self.assertRaisesRegex(gate.GateError, "oversized Marionette message"),
        ):
            gate.Marionette("127.0.0.1", 2828, 5)
        self.assertTrue(oversized.closed)


if __name__ == "__main__":
    unittest.main()
