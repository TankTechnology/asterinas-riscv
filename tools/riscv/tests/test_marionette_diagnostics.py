# SPDX-License-Identifier: MPL-2.0

import contextlib
import io
import json
import socket
import unittest
from unittest import mock

from tools.riscv.debian.rootfs import browser_m5_marionette_gate as gate


def frame(value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    return str(len(payload)).encode() + b":" + payload


class MarionetteDiagnosticsTests(unittest.TestCase):
    def client(self, enabled=True):
        client_socket, server = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client_socket.close)
        server.sendall(frame({"applicationType": "gecko", "marionetteProtocol": 3}))
        with (
            mock.patch.dict(
                gate.os.environ,
                {"ASTERINAS_MARIONETTE_DIAGNOSTICS": "1" if enabled else "0"},
            ),
            mock.patch.object(
                gate.socket, "create_connection", return_value=client_socket
            ),
        ):
            client = gate.Marionette("127.0.0.1", 2828, 0.05)
        self.addCleanup(client.close)
        return client, server

    def records(self, output):
        prefix = "A_WEB_MARIONETTE_TRANSPORT "
        return [
            json.loads(line[len(prefix) :])
            for line in output.getvalue().splitlines()
            if line.startswith(prefix)
        ]

    def test_partial_response_body_preserves_progress_without_payload(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            client, server = self.client()
            server.sendall(b"50:[1,")
            with self.assertRaises(TimeoutError):
                client.command(
                    "WebDriver:ExecuteScript",
                    {"script": "SECRET_SCRIPT", "nonce": "SECRET_NONCE"},
                )
        failures = [r for r in self.records(output) if r["event"] == "failure"]
        self.assertEqual(
            len(failures), 1, "transport timeout has no structured progress evidence"
        )
        failure = failures[0]
        self.assertEqual(failure["request_id"], 1)
        self.assertTrue(failure["send_complete"])
        self.assertEqual(failure["stage"], "response_body")
        self.assertEqual(failure["header_bytes"], 3)
        self.assertEqual(failure["body_expected"], 50)
        self.assertEqual(failure["body_received"], 3)
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("[1,", output.getvalue())
        self.assertIn(b"WebDriver:ExecuteScript", server.recv(4096))

    def test_partial_header_and_eof_are_distinct(self):
        for wire, close, stage, error_type in (
            (b"12", False, "response_header", "TimeoutError"),
            (b"12:", True, "response_body", "GateError"),
        ):
            with self.subTest(wire=wire):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    client, server = self.client()
                    server.sendall(wire)
                    if close:
                        server.shutdown(socket.SHUT_WR)
                    with self.assertRaises((gate.GateError, TimeoutError)):
                        client.command("WebDriver:Status")
                failures = [r for r in self.records(output) if r["event"] == "failure"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["stage"], stage)
                self.assertEqual(failures[0]["error_type"], error_type)

    def test_success_is_bounded_and_disabled_mode_is_silent(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    client, server = self.client(enabled)
                    response = [1, 1, None, {"value": "PRIVATE_PAGE"}]
                    server.sendall(frame(response))
                    self.assertEqual(client.command("WebDriver:GetTitle"), response[3])
                records = [r for r in self.records(output) if r["request_id"] == 1]
                self.assertNotIn("PRIVATE_PAGE", output.getvalue())
                if enabled:
                    self.assertTrue(records, "enabled diagnostics did not emit records")
                    self.assertLessEqual(len(records), 4)
                    self.assertEqual(records[-1]["event"], "complete")
                    self.assertEqual(
                        records[-1]["body_received"], records[-1]["body_expected"]
                    )
                else:
                    self.assertEqual(output.getvalue(), "")

    def test_malformed_response_identity_preserves_public_error(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            client, server = self.client()
            server.sendall(frame([1, 99, None, {}]))
            with self.assertRaisesRegex(
                gate.GateError, "unexpected Marionette response identity"
            ):
                client.command("WebDriver:Status")
        records = self.records(output)
        self.assertTrue(records, "invalid identity has no transport evidence")
        self.assertEqual(records[-1]["stage"], "response_identity")

    def test_invalid_frame_and_json_report_the_original_error(self):
        for wire, message, stage in (
            (b"x:", "invalid Marionette frame length", "response_header"),
            (b":", "missing Marionette frame length", "response_header"),
            (b"16777217:", "oversized Marionette message", "response_header"),
            (b"1:{", "invalid Marionette JSON", "response_json"),
        ):
            with self.subTest(wire=wire):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    client, server = self.client()
                    server.sendall(wire)
                    with self.assertRaisesRegex(gate.GateError, message):
                        client.command("WebDriver:Status")
                self.assertEqual(self.records(output)[-1]["stage"], stage)

    def test_fragmentation_does_not_reset_deadline_or_add_records(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            client, server = self.client()
            underlying = client._socket
            # Use the real socket, forcing one byte per recv to exercise the loop.
            fragmented = mock.Mock(wraps=underlying)
            fragmented.recv.side_effect = lambda length: underlying.recv(min(1, length))
            client._socket = fragmented
            before = client._deadline
            server.sendall(frame([1, 1, None, {"value": "ok"}]))
            self.assertEqual(client.command("WebDriver:Status"), {"value": "ok"})
            self.assertEqual(client._deadline, before)
        records = [r for r in self.records(output) if r["request_id"] == 1]
        self.assertEqual(len(records), 4)
        self.assertEqual(records[-1]["body_received"], records[-1]["body_expected"])

    def test_closed_diagnostic_sink_does_not_replace_transport_failure(self):
        with contextlib.redirect_stderr(io.StringIO()):
            client, server = self.client()
        closed = io.StringIO()
        closed.close()
        server.sendall(b"20:{")
        with contextlib.redirect_stderr(closed):
            with self.assertRaises(TimeoutError):
                client.command("WebDriver:Status")


if __name__ == "__main__":
    unittest.main()
