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

from tools.riscv.debian.rootfs import browser_m5_marionette_gate as gate


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


class DebianBrowserM5RuntimeGateTests(unittest.TestCase):
    def _snapshot(self) -> dict[str, object]:
        return {
            "url": gate.PROBE_URL,
            "markers": [list(marker) for marker in gate.EXPECTED_MARKERS],
            "media": {
                "currentSrc": gate.VIDEO_URL,
                "ended": True,
                "readyState": 4,
                "error": None,
                "duration": 0.2,
                "currentTime": 0.2,
            },
            # Firefox file:// playback may not expose media in Resource Timing.
            "resources": [],
        }

    def _pending_snapshot(self) -> dict[str, object]:
        return {
            "url": gate.PROBE_URL,
            "markers": [
                list(gate.EXPECTED_MARKERS[0]),
                list(gate.EXPECTED_MARKERS[1]),
                list(gate.PENDING_MARKERS[2]),
            ],
            "media": {
                "currentSrc": gate.VIDEO_URL,
                "ended": False,
                "readyState": 4,
                "error": None,
                "duration": 0.2,
                "currentTime": 0.1,
            },
            "resources": [],
        }

    def test_accepts_only_exact_ordered_permanent_dom_and_local_resource(self) -> None:
        gate.validate_snapshot(self._snapshot())
        resource_observed = self._snapshot()
        resource_observed["resources"] = [gate.VIDEO_URL]
        gate.validate_snapshot(resource_observed)
        mutations = []
        missing = self._snapshot()
        missing["markers"] = missing["markers"][:-1]
        mutations.append(missing)
        reordered = self._snapshot()
        reordered["markers"] = list(reversed(reordered["markers"]))
        mutations.append(reordered)
        duplicate = self._snapshot()
        duplicate["markers"] = duplicate["markers"] + [duplicate["markers"][-1]]
        mutations.append(duplicate)
        forged = self._snapshot()
        forged["markers"][1][1] = "ASTERINAS_BROWSER_M5_VIDEO_CANPLAY-ish"
        mutations.append(forged)
        external = self._snapshot()
        external["resources"] = [gate.VIDEO_URL, "https://example.invalid/tracker"]
        mutations.append(external)
        external_media = self._snapshot()
        external_media["media"]["currentSrc"] = "https://example.invalid/video.webm"
        mutations.append(external_media)
        not_ended = self._snapshot()
        not_ended["media"]["ended"] = False
        mutations.append(not_ended)
        insufficient_data = self._snapshot()
        insufficient_data["media"]["readyState"] = 1
        mutations.append(insufficient_data)
        decode_error = self._snapshot()
        decode_error["media"]["error"] = 3
        mutations.append(decode_error)
        incomplete_playback = self._snapshot()
        incomplete_playback["media"]["currentTime"] = 0.1
        mutations.append(incomplete_playback)
        wrong_page = self._snapshot()
        wrong_page["url"] = "https://example.invalid/index.html"
        mutations.append(wrong_page)
        for snapshot in mutations:
            with self.subTest(snapshot=snapshot), self.assertRaises(gate.GateError):
                gate.validate_snapshot(snapshot)

    def test_accepts_observed_firefox_esr_ended_media_shape(self) -> None:
        observed = self._snapshot()
        observed["media"].update({
            "ended": True,
            "readyState": 2,
            "duration": 1,
            "currentTime": 1,
        })
        observed["resources"] = []
        gate.validate_snapshot(observed)

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

    @mock.patch.object(gate, "Marionette", _Client)
    def test_runner_reads_real_content_context_and_closes_session(self) -> None:
        snapshot = self._snapshot()
        _Client.responses = [
            {"sessionId": "session", "capabilities": {}},
            None,
            ["wrong-window", "probe-window"],
            None,
            {"value": json.dumps({"url": "about:blank", "markers": [], "media": None, "resources": []})},
            None,
            {"value": json.dumps(snapshot)},
            {"value": None},
        ]
        gate.run_gate("127.0.0.1", 2828, 5)
        client = _Client.instance
        self.assertIsNotNone(client)
        self.assertTrue(client.closed)
        names = [name for name, _ in client.commands]
        self.assertEqual(
            names,
            [
                "WebDriver:NewSession",
                "WebDriver:Navigate",
                "WebDriver:GetWindowHandles",
                "WebDriver:SwitchToWindow",
                "WebDriver:ExecuteScript",
                "WebDriver:SwitchToWindow",
                "WebDriver:ExecuteScript",
                "WebDriver:DeleteSession",
            ],
        )
        navigate = next(parameters for name, parameters in client.commands if name == "WebDriver:Navigate")
        self.assertEqual(navigate, {"url": gate.PROBE_URL})

    @mock.patch.object(gate, "Marionette", _Client)
    def test_pre_title_diagnostic_reads_status_without_creating_session(self) -> None:
        diagnostic = {"ready": False, "message": "Firefox is still starting"}
        _Client.responses = [diagnostic]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(gate.status_once("127.0.0.1", 2828, 5), diagnostic)
        client = _Client.instance
        self.assertIsNotNone(client)
        self.assertTrue(client.closed)
        names = [name for name, _ in client.commands]
        self.assertEqual(names, ["WebDriver:Status"])
        markers = stderr.getvalue()
        expected = ("tcp-connect", "greeting", "status")
        for phase in expected:
            self.assertIn(f"A_M5_PHASE phase={phase} state=start", markers)
            self.assertIn(f"A_M5_PHASE phase={phase} state=done", markers)
        self.assertNotIn("phase=new-session", markers)
        self.assertIn("monotonic_ns=", markers)
        self.assertIn("wall_ns=", markers)

    @mock.patch.object(gate, "Marionette")
    def test_pre_title_diagnostic_attributes_protocol_timeout(self, marionette: mock.Mock) -> None:
        client = marionette.return_value
        client.command.side_effect = socket.timeout("stalled")
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaisesRegex(gate.GateError, "during status"):
            gate.status_once("127.0.0.1", 2828, 5)
        self.assertTrue(client.close.called)
        self.assertIn(
            "A_M5_PHASE phase=status state=exception", stderr.getvalue()
        )
        self.assertIn("exception_type=TimeoutError", stderr.getvalue())

    @mock.patch.object(gate, "Marionette", _Client)
    def test_pre_title_diagnostic_rejects_malformed_status(self) -> None:
        _Client.responses = [{"message": "missing ready"}]
        with self.assertRaisesRegex(gate.GateError, "invalid readiness status"):
            gate.status_once("127.0.0.1", 2828, 5)
        self.assertTrue(_Client.instance.closed)

    def test_diagnose_cli_preserves_ready_false_as_retryable_evidence(self) -> None:
        output = io.StringIO()
        status = {"ready": False, "message": "Browser startup is incomplete"}
        with (
            mock.patch.object(gate, "validate_network_namespace"),
            mock.patch.object(gate, "status_once", return_value=status),
            redirect_stdout(output),
        ):
            self.assertEqual(
                gate.main(["--firefox-pid", "42", "--diagnose-once"]), 0
            )
        self.assertEqual(
            output.getvalue(),
            'DEBIAN_BROWSER_M5_DIAGNOSTIC ready=false status='
            '{"message":"Browser startup is incomplete","ready":false}\n',
        )

    def test_formal_cli_accepts_measured_600_second_budget_only(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(gate, "validate_network_namespace"),
            mock.patch.object(gate, "run_gate") as run_gate,
            redirect_stdout(output),
        ):
            self.assertEqual(
                gate.main(["--firefox-pid", "42", "--timeout", "600"]), 0
            )
        run_gate.assert_called_once_with("127.0.0.1", 2828, 600.0)
        self.assertEqual(output.getvalue(), gate.PASS_LINE + "\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gate.main(["--firefox-pid", "42", "--timeout", "601"])

    @mock.patch.object(gate.time, "sleep", return_value=None)
    def test_runner_retries_only_loopback_connection_refused(self, _sleep: mock.Mock) -> None:
        snapshot = self._snapshot()
        _Client.responses = [
            {"sessionId": "session", "capabilities": {}},
            None, ["probe"], None, {"value": json.dumps(snapshot)}, {"value": None},
        ]
        attempts = [ConnectionRefusedError(errno.ECONNREFUSED, "not ready"), _Client]

        def connect(host: str, port: int, timeout: float) -> _Client:
            attempt = attempts.pop(0)
            if isinstance(attempt, BaseException):
                raise attempt
            return attempt(host, port, timeout)

        with mock.patch.object(gate, "Marionette", side_effect=connect):
            gate.run_gate("127.0.0.1", 2828, 5)
        self.assertEqual(_sleep.call_count, 1)
        self.assertTrue(_Client.instance.closed)

        with (
            mock.patch.object(gate, "Marionette", side_effect=ConnectionResetError()),
            self.assertRaises(ConnectionResetError),
        ):
            gate.run_gate("127.0.0.1", 2828, 5)
        malformed = mock.Mock(side_effect=gate.GateError("unexpected greeting"))
        with mock.patch.object(gate, "Marionette", malformed), self.assertRaises(gate.GateError):
            gate.run_gate("127.0.0.1", 2828, 5)
        malformed.assert_called_once()

    @mock.patch.object(gate, "Marionette", _Client)
    @mock.patch.object(gate.time, "sleep", return_value=None)
    def test_runner_polls_from_pending_to_pass(self, _sleep: mock.Mock) -> None:
        _Client.responses = [
            {"sessionId": "session", "capabilities": {}},
            None,
            ["probe"], None, {"value": json.dumps(self._pending_snapshot())},
            ["probe"], None, {"value": json.dumps(self._snapshot())},
            {"value": None},
        ]
        gate.run_gate("127.0.0.1", 2828, 5)
        names = [name for name, _ in _Client.instance.commands]
        self.assertEqual(names.count("WebDriver:ExecuteScript"), 2)
        self.assertEqual(names[-1], "WebDriver:DeleteSession")

    def test_runner_times_out_when_markers_remain_pending(self) -> None:
        pending = self._pending_snapshot()

        class PendingClient(_Client):
            def command(self, name: str, parameters: object | None = None) -> object:
                self.commands.append((name, parameters))
                if name == "WebDriver:NewSession":
                    return {"sessionId": "session", "capabilities": {}}
                if name == "WebDriver:GetWindowHandles":
                    return ["probe"]
                if name == "WebDriver:ExecuteScript":
                    return {"value": json.dumps(pending)}
                return None

        with (
            mock.patch.object(gate, "Marionette", PendingClient),
            self.assertRaisesRegex(gate.GateError, "deadline"),
        ):
            gate.run_gate("127.0.0.1", 2828, 0.001)
        names = [name for name, _ in PendingClient.instance.commands]
        self.assertNotIn("WebDriver:DeleteSession", names)
        self.assertTrue(PendingClient.instance.closed)

    @mock.patch.object(gate, "Marionette", _Client)
    def test_runner_rejects_duplicate_probe_windows(self) -> None:
        encoded = {"value": json.dumps(self._snapshot())}
        _Client.responses = [
            {"sessionId": "session", "capabilities": {}}, None, ["one", "two"],
            None, encoded, None, encoded,
        ]
        with self.assertRaisesRegex(gate.GateError, "exactly one"):
            gate.run_gate("127.0.0.1", 2828, 5)
        self.assertTrue(_Client.instance.closed)

    def test_runner_propagates_explicit_navigate_timeout(self) -> None:
        class NavigateTimeoutClient(_Client):
            def command(self, name: str, parameters: object | None = None) -> object:
                self.commands.append((name, parameters))
                if name == "WebDriver:NewSession":
                    return {"sessionId": "session", "capabilities": {}}
                if name == "WebDriver:Navigate":
                    raise socket.timeout("navigate stalled")
                raise AssertionError(f"unexpected command after Navigate: {name}")

        with (
            mock.patch.object(gate, "Marionette", NavigateTimeoutClient),
            self.assertRaisesRegex(socket.timeout, "navigate stalled"),
        ):
            gate.run_gate("127.0.0.1", 2828, 5)
        self.assertEqual(
            NavigateTimeoutClient.instance.commands,
            [
                ("WebDriver:NewSession", {"strictFileInteractability": True}),
                ("WebDriver:Navigate", {"url": gate.PROBE_URL}),
            ],
        )
        self.assertTrue(NavigateTimeoutClient.instance.closed)

    @mock.patch.object(gate, "Marionette", _Client)
    def test_runner_rejects_non_null_navigate_result(self) -> None:
        _Client.responses = [
            {"sessionId": "session", "capabilities": {}},
            {"unexpected": True},
        ]
        with self.assertRaisesRegex(gate.GateError, "invalid Navigate result"):
            gate.run_gate("127.0.0.1", 2828, 5)
        self.assertTrue(_Client.instance.closed)



if __name__ == "__main__":
    unittest.main()
