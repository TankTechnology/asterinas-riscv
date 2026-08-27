#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.riscv.debian.rootfs import browser_m5_marionette_gate as gate
from tools.riscv.debian.rootfs.profiles import get_profile


class _Client:
    responses: list[object] = []
    instance: "_Client | None" = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.connection = (host, port, timeout)
        self.commands: list[tuple[str, object | None]] = []
        self.closed = False
        type(self).instance = self

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
        with mock.patch.object(gate.socket, "create_connection", return_value=transport):
            client = gate.Marionette("127.0.0.1", 2828, 5)
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
                "WebDriver:GetWindowHandles",
                "WebDriver:SwitchToWindow",
                "WebDriver:ExecuteScript",
                "WebDriver:SwitchToWindow",
                "WebDriver:ExecuteScript",
                "WebDriver:DeleteSession",
            ],
        )

    @mock.patch.object(gate.time, "sleep", return_value=None)
    def test_runner_retries_only_loopback_connection_refused(self, _sleep: mock.Mock) -> None:
        snapshot = self._snapshot()
        _Client.responses = [
            {"sessionId": "session", "capabilities": {}},
            ["probe"], None, {"value": json.dumps(snapshot)}, {"value": None},
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
            {"sessionId": "session", "capabilities": {}}, ["one", "two"],
            None, encoded, None, encoded,
        ]
        with self.assertRaisesRegex(gate.GateError, "exactly one"):
            gate.run_gate("127.0.0.1", 2828, 5)
        self.assertTrue(_Client.instance.closed)

    def test_runtime_contract_uses_marionette_not_bidi_or_window_title_as_content(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        rootfs = repository / "tools/riscv/debian/rootfs"
        session = (rootfs / "desktop_m5_session.sh").read_text()
        firefox = (rootfs / "browser_m5_firefox.sh").read_text()
        evidence = (rootfs / "desktop_m5_evidence.sh").read_text()
        client = (rootfs / "browser_m5_marionette_gate.py").read_text()
        self.assertNotIn("firefox-esr", session)
        self.assertIn("--marionette", firefox)
        self.assertNotIn("--remote-debugging-port", firefox)
        self.assertIn('"WebDriver:ExecuteScript"', client)
        self.assertIn("video.currentSrc", client)
        self.assertIn("video.readyState", client)
        self.assertIn("video.error", client)
        self.assertIn("video.currentTime", client)
        self.assertNotIn("script.evaluate", client)
        self.assertIn("browser-m5-marionette-gate", evidence)
        self.assertIn('remaining=$((deadline - SECONDS))', evidence)
        self.assertIn('gate_timeout="$remaining"', evidence)
        self.assertIn("network_mode=private-loopback source=file", gate.PASS_LINE)
        self.assertIn("direct_nonloopback_ip=unavailable", gate.PASS_LINE)
        self.assertNotIn("network=offline", gate.PASS_LINE)
        self.assertLess(evidence.index("DEBIAN_BROWSER_M5_WORKLOAD"), evidence.index('emit "$content_evidence"'))

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
