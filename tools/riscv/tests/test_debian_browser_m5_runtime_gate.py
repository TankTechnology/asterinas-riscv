#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
from pathlib import Path
import socket
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
        evidence = (rootfs / "desktop_m5_evidence.sh").read_text()
        client = (rootfs / "browser_m5_marionette_gate.py").read_text()
        self.assertIn("--marionette", session)
        self.assertNotIn("--remote-debugging-port", session)
        self.assertIn('"WebDriver:ExecuteScript"', client)
        self.assertIn("video.currentSrc", client)
        self.assertIn("video.readyState", client)
        self.assertIn("video.error", client)
        self.assertIn("video.currentTime", client)
        self.assertNotIn("script.evaluate", client)
        self.assertIn("browser-m5-marionette-gate", evidence)
        self.assertLess(evidence.index("DEBIAN_BROWSER_M5_WORKLOAD"), evidence.index('emit "$content_evidence"'))

    def test_profile_guarantees_small_stdlib_client_runtime(self) -> None:
        profile = get_profile("browser-m5")
        self.assertIn("python3-minimal", profile.requested_packages)
        self.assertIn("python3-minimal", profile.identity_packages)
        self.assertNotIn("geckodriver", profile.requested_packages)
        self.assertNotIn("selenium", profile.requested_packages)


if __name__ == "__main__":
    unittest.main()
