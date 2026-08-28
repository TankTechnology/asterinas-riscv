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
from tools.riscv.debian.rootfs.profiles import get_profile


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

    def test_runtime_contract_uses_marionette_not_bidi_or_window_title_as_content(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        rootfs = repository / "tools/riscv/debian/rootfs"
        session = (rootfs / "desktop_m5_session.sh").read_text()
        firefox = (rootfs / "browser_m5_firefox.sh").read_text()
        observer = (rootfs / "browser_m5_window_observer.sh").read_text()
        rootfs_builder = (rootfs / "build_rootfs.sh").read_text()
        evidence = (rootfs / "desktop_m5_evidence.sh").read_text()
        client = (rootfs / "browser_m5_marionette_gate.py").read_text()
        self.assertNotIn("firefox-esr", session)
        self.assertIn("--marionette", firefox)
        self.assertIn("browser-m5-window-observer", firefox)
        self.assertIn("browser_m5_window_observer.sh", rootfs_builder)
        self.assertIn("ASTERINAS_FIREFOX_X11_NAVIGATOR_VISIBLE", observer)
        self.assertIn("ASTERINAS_FIREFOX_X11_WINDOW_READY", observer)
        self.assertIn("SAMPLE_LIMIT:-240", observer)
        self.assertIn("WINDOW_TIMEOUT_SECONDS:-4500", observer)
        self.assertNotIn("--remote-debugging-port", firefox)
        self.assertIn('"WebDriver:ExecuteScript"', client)
        self.assertIn("video.currentSrc", client)
        self.assertIn("video.readyState", client)
        self.assertIn("video.error", client)
        self.assertIn("video.currentTime", client)
        self.assertNotIn("script.evaluate", client)
        self.assertIn("browser-m5-marionette-gate", evidence)
        self.assertIn("--diagnose-once", evidence)
        self.assertLess(evidence.index("--diagnose-once"), evidence.index('content_evidence="$('))
        self.assertIn("((diagnostic_timeout <= 30)) || diagnostic_timeout=30", evidence)
        self.assertIn("FORMAL_GATE_TIMEOUT_SECONDS:-600", evidence)
        self.assertIn("gate_timeout <= FORMAL_GATE_TIMEOUT_SECONDS", evidence)
        self.assertNotIn("diagnostic_emitted", evidence)
        self.assertIn("while ! ready || ! navigator_ready", evidence)
        self.assertIn("NavigatorWindowReady", evidence)
        self.assertIn('DEBIAN_BROWSER_M5_DIAGNOSTIC ready=true status=', evidence)
        self.assertEqual(evidence.count("marionette_ready=true"), 1)
        ready_assignment = evidence.index("marionette_ready=true")
        self.assertLess(
            evidence.rindex("DEBIAN_BROWSER_M5_DIAGNOSTIC ready=true status=", 0, ready_assignment),
            ready_assignment,
        )
        unavailable = evidence.index('emit "DEBIAN_BROWSER_M5_DIAGNOSTIC status=unavailable"')
        self.assertNotIn("marionette_ready=true", evidence[unavailable:evidence.index("fi", unavailable)])
        self.assertNotIn("asterinas offline browser m5 probe", evidence.lower())
        self.assertIn('remaining=$((deadline - SECONDS))', evidence)
        self.assertIn('gate_timeout="$remaining"', evidence)
        self.assertIn("network_mode=private-loopback source=file", gate.PASS_LINE)
        self.assertIn("direct_nonloopback_ip=unavailable", gate.PASS_LINE)
        self.assertNotIn("network=offline", gate.PASS_LINE)
        self.assertLess(evidence.index("DEBIAN_BROWSER_M5_WORKLOAD"), evidence.index('emit "$content_evidence"'))

    def test_window_observer_ignores_tiny_probe_and_survives_past_old_limit(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        observer = repository / "tools/riscv/debian/rootfs/browser_m5_window_observer.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_sleep = root / "sleep"
            fake_sync = root / "sync"
            fake_xwininfo = root / "xwininfo"
            counter = root / "counter"
            console = root / "console"
            window_log = root / "window.log"
            navigator = root / "NavigatorWindowReady"
            fake_sleep.write_text("#!/bin/sh\nexit 0\n")
            fake_sync.write_text("#!/bin/sh\nexit 0\n")
            fake_xwininfo.write_text(
                "#!/bin/sh\n"
                "count=$(($(cat \"$ASTERINAS_M5_TEST_COUNTER\" 2>/dev/null || printf 0) + 1))\n"
                "printf '%s\\n' \"$count\" >\"$ASTERINAS_M5_TEST_COUNTER\"\n"
                "case $count in\n"
                "  1) printf '%s\\n' '0x800001 \"Firefox\": (\"firefox-esr\" \"Firefox-esr\")  10x10+10+10' ;;\n"
                "  2) printf '%s\\n' '0x800001 \"New Tab - Mozilla Firefox\": (\"firefox-esr\" \"Navigator\")  1280x1024+0+0' ;;\n"
                "  *) printf '%s\\n' '0x800001 \"Asterinas Offline Browser M5 Probe - Mozilla Firefox\": (\"firefox-esr\" \"Navigator\")  1280x1024+0+0' ;;\n"
                "esac\n"
            )
            for executable in (fake_sleep, fake_sync, fake_xwininfo):
                executable.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(observer)],
                cwd=repository,
                env={
                    **os.environ,
                    "ASTERINAS_BROWSER_M5_PARENT_PID": "0",
                    "ASTERINAS_BROWSER_M5_WINDOW_CONSOLE": str(console),
                    "ASTERINAS_BROWSER_M5_WINDOW_LOG": str(window_log),
                    "ASTERINAS_BROWSER_M5_NAVIGATOR_READY_FILE": str(navigator),
                    "ASTERINAS_BROWSER_M5_WINDOW_SAMPLE_SECONDS": "1",
                    "ASTERINAS_BROWSER_M5_WINDOW_SAMPLE_LIMIT": "61",
                    "ASTERINAS_BROWSER_M5_SLEEP_COMMAND": str(fake_sleep),
                    "ASTERINAS_BROWSER_M5_SYNC_COMMAND": str(fake_sync),
                    "ASTERINAS_BROWSER_M5_XWININFO_COMMAND": str(fake_xwininfo),
                    "ASTERINAS_M5_TEST_COUNTER": str(counter),
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stderr + "\nconsole:\n" + console.read_text(),
            )
            markers = console.read_text().splitlines()
            self.assertEqual(len(markers), 2)
            self.assertIn("X11_NAVIGATOR_VISIBLE", markers[0])
            self.assertIn("sequence=2", markers[0])
            self.assertIn("geometry=1280x1024", markers[0])
            self.assertIn("X11_WINDOW_READY", markers[1])
            self.assertIn("sequence=3", markers[1])
            self.assertNotIn("WINDOW_TIMEOUT", console.read_text())
            self.assertEqual(counter.read_text(), "3\n")
            self.assertIn("browser_pid=0 sequence=2", navigator.read_text())

            counter.unlink()
            console.unlink()
            navigator.write_text("browser_pid=999 stale=true\n")
            fake_xwininfo.write_text("#!/bin/sh\nprintf '%s\\n' 'root has no clients'\n")
            fake_xwininfo.chmod(0o755)
            retry = subprocess.run(
                ["/bin/bash", str(observer)],
                cwd=repository,
                env={
                    **os.environ,
                    "ASTERINAS_BROWSER_M5_PARENT_PID": "0",
                    "ASTERINAS_BROWSER_M5_WINDOW_CONSOLE": str(console),
                    "ASTERINAS_BROWSER_M5_WINDOW_LOG": str(window_log),
                    "ASTERINAS_BROWSER_M5_NAVIGATOR_READY_FILE": str(navigator),
                    "ASTERINAS_BROWSER_M5_WINDOW_SAMPLE_SECONDS": "1",
                    "ASTERINAS_BROWSER_M5_WINDOW_SAMPLE_LIMIT": "61",
                    "ASTERINAS_BROWSER_M5_SLEEP_COMMAND": str(fake_sleep),
                    "ASTERINAS_BROWSER_M5_SYNC_COMMAND": str(fake_sync),
                    "ASTERINAS_BROWSER_M5_XWININFO_COMMAND": str(fake_xwininfo),
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertFalse(navigator.exists())
            self.assertIn(
                "X11_WINDOW_TIMEOUT reason=sample-limit samples=61",
                console.read_text(),
            )

    def test_evidence_retries_failure_and_ready_false_before_one_formal_gate(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        evidence = repository / "tools/riscv/debian/rootfs/desktop_m5_evidence.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            proc = root / "proc"
            profile = root / "profile"
            inputs = root / "input"
            console = root / "console"
            xorg = root / "Xorg.0.log"
            calls = root / "gate-calls"
            gate_args = root / "gate-args"
            process_samples = root / "process-samples"
            kernel_samples = root / "kernel-samples"
            security_log = root / "security"
            navigator_ready = profile / "NavigatorWindowReady"
            fake_bin.mkdir()
            (proc / "42").mkdir(parents=True)
            profile.mkdir()
            inputs.mkdir()
            calls.touch()
            (inputs / "event0").touch()
            (inputs / "event1").touch()
            (proc / "42/comm").write_text("firefox-esr\n")
            (proc / "42/status").write_text(
                "Name:\tfirefox-esr\n"
                "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
                "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n"
                "CapAmb:\t0000000000000000\nNoNewPrivs:\t1\n"
            )
            (proc / "42/environ").write_bytes(b"HOME=/home/asterinas\0MOZ_SANDBOX_LOGGING=1\0")
            (proc / "42/cmdline").write_bytes(
                b"/usr/bin/firefox-esr\0--offline\0--marionette\0"
                b"file:///usr/share/asterinas/browser-m5/index.html\0"
            )
            # Shape copied from the real m5f19b PID 1265 tab-content command line.
            (proc / "43").mkdir()
            (proc / "43/comm").write_text("MainThread\n")
            (proc / "43/cmdline").write_bytes(
                b"/usr/lib/firefox-esr/firefox-esr\0-contentproc\0-isForBrowser\0"
                b"-prefsHandle\0" b"0:34195\0-parentBuildID\0" b"20260811190631\0"
                b"-ipcHandle\0" b"3\0-parentPid\0" b"42\0-appDir\0/usr/lib/firefox-esr/browser\0"
                b"2\0tab\0"
            )
            (proc / "43/status").write_text(
                "Name:\tMainThread\n"
                "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
                "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n"
                "CapAmb:\t0000000000000000\nSeccomp:\t2\n"
            )
            (profile / "MarionetteActivePort").write_text("2828\n")
            xorg.write_text(
                "FBDEV(0)\n"
                "Adding extended input device test Asterinas keyboard\n"
                "Adding extended input device test Asterinas pointer\n"
            )
            (fake_bin / "systemctl").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = show ]; then printf '42\\n'; fi\n"
                "exit 0\n"
            )
            (fake_bin / "loginctl").write_text(
                "#!/bin/sh\nprintf 'c1 1000 asterinas seat0 tty1\\n'\n"
            )
            (fake_bin / "pgrep").write_text("#!/bin/sh\nexit 0\n")
            (fake_bin / "dmesg").write_text("#!/bin/sh\nexit 0\n")
            fake_gate = fake_bin / "browser-m5-marionette-gate"
            fake_gate.write_text(
                "#!/bin/sh\n"
                "if printf '%s' \"$*\" | grep -q -- --diagnose-once; then\n"
                "  count=$(wc -l <\"$ASTERINAS_M5_TEST_CALLS\")\n"
                "  case $count in\n"
                "    0) printf 'diagnose-timeout\\n' >>\"$ASTERINAS_M5_TEST_CALLS\"; exit 2 ;;\n"
                "    1) printf 'diagnose-false\\n' >>\"$ASTERINAS_M5_TEST_CALLS\"; "
                "printf '%s\\n' 'DEBIAN_BROWSER_M5_DIAGNOSTIC ready=false status={\"message\":\"starting\",\"ready\":false}' ;;\n"
                "    *) printf 'diagnose-false-window\\n' >>\"$ASTERINAS_M5_TEST_CALLS\"; "
                "printf 'browser_pid=42 sequence=61 seconds=1830\\n' >\"$ASTERINAS_M5_TEST_NAVIGATOR\"; "
                "printf '%s\\n' 'DEBIAN_BROWSER_M5_DIAGNOSTIC ready=false status={\"message\":\"window visible\",\"ready\":false}' ;;\n"
                "  esac\n"
                "else\n"
                "  printf 'formal\\n' >>\"$ASTERINAS_M5_TEST_CALLS\"\n"
                "  printf '%s\\n' \"$*\" >\"$ASTERINAS_M5_TEST_GATE_ARGS\"\n"
                f"  printf '%s\\n' '{gate.PASS_LINE}'\n"
                "fi\n"
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            result = subprocess.run(
                ["/bin/bash", str(evidence)],
                cwd=repository,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "ASTERINAS_DESKTOP_M5_CONSOLE": str(console),
                    "ASTERINAS_DESKTOP_M5_INPUT_DIRECTORY": str(inputs),
                    "ASTERINAS_DESKTOP_M5_XORG_LOG": str(xorg),
                    "ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS": "700",
                    "ASTERINAS_DESKTOP_M5_PROC_ROOT": str(proc),
                    "ASTERINAS_DESKTOP_M5_PROFILE_DIRECTORY": str(profile),
                    "ASTERINAS_DESKTOP_M5_CONTENT_GATE": str(fake_gate),
                    "ASTERINAS_DESKTOP_M5_PROCESS_SAMPLE_LOG": str(process_samples),
                    "ASTERINAS_DESKTOP_M5_KERNEL_SAMPLE_LOG": str(kernel_samples),
                    "ASTERINAS_DESKTOP_M5_SECURITY_LOG": str(security_log),
                    "ASTERINAS_M5_TEST_CALLS": str(calls),
                    "ASTERINAS_M5_TEST_GATE_ARGS": str(gate_args),
                    "ASTERINAS_M5_TEST_NAVIGATOR": str(navigator_ready),
                },
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                result.returncode,
                0,
                result.stderr + "\nconsole:\n" + console.read_text(),
            )
            self.assertEqual(
                calls.read_text().splitlines(),
                ["diagnose-timeout", "diagnose-false", "diagnose-false-window", "formal"],
            )
            emitted = console.read_text()
            self.assertIn("DEBIAN_BROWSER_M5_DIAGNOSTIC status=unavailable", emitted)
            self.assertIn("DEBIAN_BROWSER_M5_DIAGNOSTIC ready=false", emitted)
            self.assertNotIn("DEBIAN_BROWSER_M5_DIAGNOSTIC ready=true", emitted)
            self.assertIn(
                "DEBIAN_BROWSER_M5_NAVIGATOR state=visible browser_pid=42 "
                "marionette_status_ready=false",
                emitted,
            )
            self.assertEqual(emitted.count(gate.PASS_LINE), 1)
            self.assertIn("--timeout 600", gate_args.read_text())
            self.assertIn("DEBIAN_BROWSER_M5_SECURITY parent_caps=zero", emitted)
            self.assertIn(
                "DEBIAN_BROWSER_M5_SECURITY child_caps=zero content=present seccomp=enabled",
                emitted,
            )
            self.assertIn("role=content pid=43 capabilities=zero", security_log.read_text())
            samples = process_samples.read_text()
            self.assertEqual(samples.count("stage=formal-gate-start"), 1)
            self.assertEqual(samples.count("stage=formal-gate-done"), 1)

            status_without_seccomp = (proc / "43/status").read_text().replace(
                "Seccomp:\t2\n", ""
            )
            (proc / "43/status").write_text(status_without_seccomp)
            calls.write_text("")
            console.write_text("")
            navigator_ready.unlink()
            rejected = subprocess.run(
                ["/bin/bash", str(evidence)],
                cwd=repository,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "ASTERINAS_DESKTOP_M5_CONSOLE": str(console),
                    "ASTERINAS_DESKTOP_M5_INPUT_DIRECTORY": str(inputs),
                    "ASTERINAS_DESKTOP_M5_XORG_LOG": str(xorg),
                    "ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS": "700",
                    "ASTERINAS_DESKTOP_M5_PROC_ROOT": str(proc),
                    "ASTERINAS_DESKTOP_M5_PROFILE_DIRECTORY": str(profile),
                    "ASTERINAS_DESKTOP_M5_CONTENT_GATE": str(fake_gate),
                    "ASTERINAS_DESKTOP_M5_PROCESS_SAMPLE_LOG": str(process_samples),
                    "ASTERINAS_DESKTOP_M5_KERNEL_SAMPLE_LOG": str(kernel_samples),
                    "ASTERINAS_DESKTOP_M5_SECURITY_LOG": str(security_log),
                    "ASTERINAS_M5_TEST_CALLS": str(calls),
                    "ASTERINAS_M5_TEST_GATE_ARGS": str(gate_args),
                    "ASTERINAS_M5_TEST_NAVIGATOR": str(navigator_ready),
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn(
                "DEBIAN_BROWSER_M5_FAIL reason=security-content-seccomp-unavailable",
                console.read_text(),
            )

    def test_evidence_samples_formal_gate_failure_before_exit(self) -> None:
        evidence = (
            Path(__file__).resolve().parents[3]
            / "tools/riscv/debian/rootfs/desktop_m5_evidence.sh"
        ).read_text()
        gate_call = evidence.index('if ! content_evidence="$("$CONTENT_GATE"')
        failure_sample = evidence.index(
            "sample_firefox_processes formal-gate-failed", gate_call
        )
        failure_exit = evidence.index("fail browser-content", failure_sample)
        self.assertLess(gate_call, failure_sample)
        self.assertLess(failure_sample, failure_exit)

    def test_security_rechecks_restarted_firefox_and_requires_content_seccomp(self) -> None:
        evidence = (
            Path(__file__).resolve().parents[3]
            / "tools/riscv/debian/rootfs/desktop_m5_evidence.sh"
        ).read_text()
        self.assertIn('security_checked_pid=""', evidence)
        self.assertIn('[[ "$security_checked_pid" != "$browser_pid" ]]', evidence)
        self.assertIn('security_checked_pid="$browser_pid"', evidence)
        self.assertNotIn("security_checked=false", evidence)
        self.assertIn("fail security-content-seccomp-unavailable", evidence)
        self.assertIn("fail security-content-seccomp", evidence)

    def test_network_namespace_contract_accepts_only_same_loopback_namespace(self) -> None:
        with mock.patch.object(gate.os, "readlink", side_effect=["net:[7]", "net:[7]"]), \
             mock.patch.object(gate.socket, "if_nameindex", return_value=[(1, "lo")]):
            gate.validate_network_namespace(123)

        with mock.patch.object(gate.os, "readlink", side_effect=["net:[7]", "net:[8]"]), \
             mock.patch.object(gate.socket, "if_nameindex", return_value=[(1, "lo")]):
            with self.assertRaisesRegex(gate.GateError, "did not join"):
                gate.validate_network_namespace(123)

        with mock.patch.object(gate.os, "readlink", side_effect=["net:[7]", "net:[7]"]), \
             mock.patch.object(gate.socket, "if_nameindex", return_value=[(1, "lo"), (2, "eth0")]):
            with self.assertRaisesRegex(gate.GateError, "loopback-only"):
                gate.validate_network_namespace(123)

    def test_network_observer_accepts_offline_initial_namespace_without_eth0(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        observer = repository / "tools/riscv/debian/rootfs/browser_m5_network_observer.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            fake_bin = root / "bin"
            console = root / "console"
            (proc / "self/ns").mkdir(parents=True)
            (proc / "42/ns").mkdir(parents=True)
            fake_bin.mkdir()
            (proc / "initial-netns").touch()
            (proc / "browser-netns").touch()
            (proc / "self/ns/net").symlink_to(proc / "initial-netns")
            (proc / "42/ns/net").symlink_to(proc / "browser-netns")
            (proc / "42/comm").write_text("firefox-esr\n")
            systemctl = fake_bin / "systemctl"
            systemctl.write_text("#!/bin/sh\nprintf '42\\n'\n")
            systemctl.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(observer)],
                cwd=repository,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "ASTERINAS_DESKTOP_M5_CONSOLE": str(console),
                    "ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS": "0",
                    "ASTERINAS_BROWSER_M5_PROC_ROOT": str(proc),
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                console.read_text(),
                "DEBIAN_BROWSER_M5_NETNS firefox=private initial=distinct\n",
            )

    def test_network_observer_waits_past_sixty_seconds_for_firefox_exec(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        observer = repository / "tools/riscv/debian/rootfs/browser_m5_network_observer.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            fake_bin = root / "bin"
            console = root / "console"
            fake_seconds = root / "fake-seconds"
            bash_environment = root / "bash-environment"
            (proc / "self/ns").mkdir(parents=True)
            (proc / "42/ns").mkdir(parents=True)
            fake_bin.mkdir()
            (proc / "initial-netns").touch()
            (proc / "browser-netns").touch()
            (proc / "self/ns/net").symlink_to(proc / "initial-netns")
            (proc / "42/ns/net").symlink_to(proc / "browser-netns")
            (proc / "42/comm").write_text("(sd-executor)\n")
            systemctl = fake_bin / "systemctl"
            systemctl.write_text("#!/bin/sh\nprintf '42\\n'\n")
            systemctl.chmod(0o755)
            bash_environment.write_text(
                """sleep() {
    SECONDS=$((SECONDS + $1))
    printf '%s\\n' "$SECONDS" >"$ASTERINAS_BROWSER_M5_FAKE_SECONDS"
    if ((SECONDS > 60)); then
        printf '%s\\n' firefox-esr >"$ASTERINAS_BROWSER_M5_PROC_ROOT/42/comm"
    fi
}
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", str(observer)],
                cwd=repository,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "BASH_ENV": str(bash_environment),
                    "ASTERINAS_DESKTOP_M5_CONSOLE": str(console),
                    "ASTERINAS_BROWSER_M5_PROC_ROOT": str(proc),
                    "ASTERINAS_BROWSER_M5_FAKE_SECONDS": str(fake_seconds),
                },
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            elapsed = int(fake_seconds.read_text())
            self.assertGreater(elapsed, 60)
            self.assertLess(elapsed, 300)
            self.assertEqual(
                console.read_text(),
                "DEBIAN_BROWSER_M5_NETNS firefox=private initial=distinct\n",
            )

    def test_profile_guarantees_small_stdlib_client_runtime(self) -> None:
        profile = get_profile("browser-m5")
        self.assertIn("python3-minimal", profile.requested_packages)
        self.assertIn("python3-minimal", profile.identity_packages)
        self.assertNotIn("geckodriver", profile.requested_packages)
        self.assertNotIn("selenium", profile.requested_packages)


if __name__ == "__main__":
    unittest.main()
