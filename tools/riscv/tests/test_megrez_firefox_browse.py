#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.riscv import megrez_firefox_browse as browse
from tools.riscv.megrez_boot_stability import BootReadinessEvidence


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        bootargs=(
            "console=ttyS0 console=tty0 loglevel=debug init=/init "
            "asterinas.mmc_write_partition2 "
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
            "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_URL=http://10.100.19.216:17893 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=10.100.19.216 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=17893 "
            "asterinas.reboot_after=60 -- --root-init=systemd"
        ),
        plan_sha256="a" * 64,
        validate=lambda: None,
    )


def _page() -> bytes:
    return (
        json.dumps(
            {
                "url": "https://www.baidu.com/",
                "title": "百度一下，你就知道",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\nverified-screenshot"


class _Publisher:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.published = None

    def invalidate(self) -> None:
        self.events.append("invalidate")

    def publish(self, result, serial, diagnostics, page, screenshot, proxy) -> None:
        self.events.append(f"publish:{result.reason}")
        self.published = (result, serial, diagnostics, page, screenshot, proxy)


class _Proxy:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def start(self) -> None:
        self.events.append("proxy-start")
        if self.fail:
            raise RuntimeError("proxy unavailable")

    def close(self) -> None:
        self.events.append("proxy-close")

    def summary(self) -> dict[str, object]:
        return {"schema_version": 1, "upstream": "127.0.0.1:7890"}


class _Operations:
    def __init__(self, events: list[str], *, fail_at: str | None = None) -> None:
        self.events = events
        self.fail_at = fail_at
        self._guest_started = False
        self._transcript = b"physical firefox serial\n"

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    @property
    def transcript(self) -> bytes:
        return self._transcript

    def open(self, _timeout: float) -> None:
        self.events.append("open")

    def ensure_artifacts(self, _plan, _timeout: float) -> tuple[str, ...]:
        self.events.append("ensure-artifacts")
        return ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc")

    def boot(self, _plan, bootargs: str, _timeout: float) -> None:
        self.events.append("boot")
        assert "asterinas.reboot_after=1050" in bootargs
        self._guest_started = True

    def prove_boot_readiness(self, _timeout: float) -> BootReadinessEvidence:
        self.events.append("readiness")
        return BootReadinessEvidence(
            browser_pid=116,
            framebuffer=True,
            xorg_fbdev=True,
            openbox=True,
            firefox=True,
            browser_service="active",
            browser_restarts=0,
        )

    def synchronize_clock(self, browser_pid: int, _timeout: float) -> dict[str, object]:
        self.events.append("clock")
        assert browser_pid == 116
        return {
            "marker": "ASTERINAS_CLOCK_SYNC_READY",
            "source": "http-date",
            "unix_seconds": 1789099506,
        }

    def run_baidu_home(
        self, browser_pid: int, nonce: str, _timeout: float
    ) -> dict[str, object]:
        self.events.append("baidu-home")
        assert browser_pid == 116
        assert len(nonce) == 16
        if self.fail_at == "baidu-home":
            raise TimeoutError("Marionette homepage timed out")
        return {
            "marker": "DEBIAN_BROWSER_WEB_BAIDU_HOME_READY",
            "scope": "baidu-home",
            "title_sha256": hashlib.sha256("百度一下，你就知道".encode()).hexdigest(),
            "tls": "verified",
            "url": "https://www.baidu.com/",
        }

    def retrieve_evidence(self, nonce: str, name: str, _timeout: float) -> bytes:
        self.events.append(f"retrieve:{name}")
        assert len(nonce) == 16
        return _page() if name == "baidu-home.json" else _png()

    def collect_diagnostics(self, _timeout: float) -> bytes:
        self.events.append("collect-diagnostics")
        return b"bounded diagnostics\n"

    def request_reboot(self, _timeout: float) -> None:
        self.events.append("request-reboot")
        if self.fail_at == "request-reboot":
            raise TimeoutError("guest command deadline expired")

    def await_recovery(self, _timeout: float) -> None:
        self.events.append("recovery")
        self._transcript += b"U-Boot\n=> \n"

    def close(self) -> None:
        self.events.append("close")


class FirefoxBrowseTests(unittest.TestCase):
    def test_bootargs_keep_network_proxy_and_safety_reboot_without_writes(self) -> None:
        tokens = browse.firefox_browse_bootargs(_plan()).split()
        self.assertIn("asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1", tokens)
        self.assertIn(
            "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e",
            tokens,
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_URL=http://10.100.19.216:17893",
            tokens,
        )
        self.assertIn("asterinas.reboot_after=1050", tokens)
        self.assertNotIn("asterinas.reboot_after=900", tokens)
        self.assertEqual(tokens.count("asterinas.tcp_diagnostic_port=2828"), 1)
        self.assertFalse(any("mmc_write_partition2" in token for token in tokens))

    @mock.patch.object(browse, "validate_baidu_home")
    @mock.patch.object(browse, "validate_png_screenshot")
    def test_success_is_one_boot_one_session_evidence_and_safe_recovery(
        self, validate_png: mock.Mock, validate_page: mock.Mock
    ) -> None:
        validate_png.return_value = (1280, 887)
        events: list[str] = []
        operations = _Operations(events)
        publisher = _Publisher(events)
        proxy = _Proxy(events)

        result = browse.run_firefox_browse(
            _plan(),
            browse.FirefoxBrowseConfig(),
            operations,
            publisher,
            proxy,
            nonce="0123456789abcdef",
            clock=lambda: 10.0,
        )

        self.assertTrue(result.passed)
        self.assertTrue(result.recovered)
        self.assertEqual(result.physical_boots, 1)
        self.assertEqual(
            events,
            [
                "invalidate",
                "proxy-start",
                "open",
                "ensure-artifacts",
                "boot",
                "readiness",
                "clock",
                "baidu-home",
                "retrieve:baidu-home.json",
                "retrieve:baidu-home.png",
                "request-reboot",
                "recovery",
                "proxy-close",
                "publish:baidu-home-ready",
                "close",
            ],
        )
        validate_page.assert_called_once()
        validate_png.assert_called_once_with(_png(), expected_dimensions=None)
        self.assertEqual(publisher.published[3], _page())
        self.assertEqual(publisher.published[4], _png())

    @mock.patch.object(browse, "validate_baidu_home")
    @mock.patch.object(browse, "validate_png_screenshot", return_value=(800, 600))
    def test_rejects_a_tiny_browser_content_viewport(
        self, _validate_png: mock.Mock, _validate_page: mock.Mock
    ) -> None:
        events: list[str] = []
        result = browse.run_firefox_browse(
            _plan(),
            browse.FirefoxBrowseConfig(),
            _Operations(events),
            _Publisher(events),
            _Proxy(events),
            nonce="0123456789abcdef",
            clock=lambda: 10.0,
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("browser-content-viewport-is-too-small", result.failure)

    def test_failure_collects_diagnostics_then_recovers_and_closes_proxy(self) -> None:
        events: list[str] = []
        operations = _Operations(events, fail_at="baidu-home")
        publisher = _Publisher(events)

        result = browse.run_firefox_browse(
            _plan(),
            browse.FirefoxBrowseConfig(),
            operations,
            publisher,
            _Proxy(events),
            nonce="0123456789abcdef",
            clock=lambda: 10.0,
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("collect-diagnostics", events)
        self.assertLess(
            events.index("collect-diagnostics"), events.index("request-reboot")
        )
        self.assertLess(events.index("recovery"), events.index("proxy-close"))
        self.assertEqual(events[-1], "close")

    def test_expired_reboot_request_still_waits_for_kernel_recovery(self) -> None:
        events: list[str] = []
        operations = _Operations(events, fail_at="request-reboot")

        result = browse.run_firefox_browse(
            _plan(),
            browse.FirefoxBrowseConfig(),
            operations,
            _Publisher(events),
            _Proxy(events),
            nonce="0123456789abcdef",
            clock=lambda: 10.0,
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertEqual(result.reason, "baidu-home-incomplete")
        self.assertIn("reboot-timeout-error-guest-command-deadline-expired", result.failure)
        self.assertNotIn("recovery-incomplete", result.failure)
        self.assertLess(events.index("request-reboot"), events.index("recovery"))

    def test_serial_evidence_frame_is_nonce_size_hash_bound(self) -> None:
        payload = _png()
        nonce = "0123456789abcdef"
        digest = hashlib.sha256(payload).hexdigest()
        transcript = (
            f"__ASTERINAS_BROWSE_FILE_BEGIN__ nonce={nonce} "
            f"name=baidu-home.png size={len(payload)} sha256={digest}\n"
            + base64.b64encode(payload).decode()
            + "\n"
            + f"__ASTERINAS_BROWSE_FILE_END__ nonce={nonce} "
            "name=baidu-home.png status=0\n"
        )
        self.assertEqual(
            browse.parse_evidence_frame(transcript, nonce, "baidu-home.png"), payload
        )
        with self.assertRaisesRegex(Exception, "identity"):
            browse.parse_evidence_frame(
                transcript.replace(digest, "0" * 64), nonce, "baidu-home.png"
            )

    def test_real_guest_commands_use_tcp_marionette_scope_and_clock_first(self) -> None:
        operations = object.__new__(browse.RealFirefoxBrowseOperations)
        serial = mock.Mock()
        serial.checkpoint.return_value = 10
        operations._require_serial = mock.Mock(return_value=serial)
        operations._run_long_step = mock.Mock()
        operations._step_payload = mock.Mock(
            side_effect=(
                b'{"marker":"ASTERINAS_CLOCK_SYNC_READY"}\n',
                (
                    b'{"marker":"DEBIAN_BROWSER_WEB_BAIDU_HOME_READY",'
                    b'"scope":"baidu-home","title_sha256":"'
                    + b"0" * 64
                    + b'","tls":"verified","url":"https://www.baidu.com/"}\n'
                ),
            )
        )

        clock = operations.synchronize_clock(116, 45)
        home = operations.run_baidu_home(
            116,
            "0123456789abcdef",
            browse.FirefoxBrowseConfig().browse_timeout,
        )

        self.assertEqual(clock["marker"], "ASTERINAS_CLOCK_SYNC_READY")
        self.assertEqual(home["scope"], "baidu-home")
        clock_command = operations._run_long_step.call_args_list[0].args[0]
        home_command = operations._run_long_step.call_args_list[1].args[0]
        self.assertEqual(
            operations._run_long_step.call_args_list[0].kwargs["accepted_statuses"],
            ("0", "124"),
        )
        self.assertEqual(
            operations._run_long_step.call_args_list[1].kwargs["accepted_statuses"],
            ("0", "124"),
        )
        self.assertIn("megrez-clock-sync", clock_command)
        self.assertIn("nsenter -t 116 -n", clock_command)
        self.assertIn("browser-web-marionette-gate --scope baidu-home", home_command)
        self.assertIn("--firefox-pid 116", home_command)
        self.assertIn("ASTERINAS_MARIONETTE_DIAGNOSTICS=1", home_command)
        self.assertIn("ASTERINAS_MARIONETTE_DEBUG_ERRORS=1", home_command)
        # Leave about one minute before the host's 1020-second guest deadline
        # so a failed page gate can still export its bounded diagnostics.
        self.assertIn("/usr/bin/timeout 660 ", home_command)
        self.assertIn("--timeout 650 ", home_command)
        self.assertEqual(
            browse.RealFirefoxBrowseOperations.GUEST_LIFETIME_SECONDS, 1050
        )
        self.assertNotIn("/proc/net/tcp", home_command)
        self.assertNotIn("DeleteSession", home_command)
        self.assertLess(len((home_command + "\n").encode()), 768)
        self.assertNotIn(
            "/proc/net/tcp", inspect.getsource(browse.RealFirefoxBrowseOperations)
        )

    def test_clock_sync_retries_a_timeout_without_completion_evidence(self) -> None:
        operations = object.__new__(browse.RealFirefoxBrowseOperations)
        serial = mock.Mock()
        serial.checkpoint.side_effect = (10, 20)
        operations._require_serial = mock.Mock(return_value=serial)
        operations._run_long_step = mock.Mock()
        operations._step_payload = mock.Mock(
            side_effect=(
                b"",
                b'{"marker":"ASTERINAS_CLOCK_SYNC_READY"}\n',
            )
        )

        with mock.patch.object(
            browse.secrets, "token_hex", side_effect=("1" * 16, "2" * 16)
        ):
            evidence = operations.synchronize_clock(116, 45)

        self.assertEqual(evidence["marker"], "ASTERINAS_CLOCK_SYNC_READY")
        self.assertEqual(operations._run_long_step.call_count, 2)
        self.assertEqual(
            [call.args[1] for call in operations._run_long_step.call_args_list],
            ["clock-1", "clock-2"],
        )
        self.assertEqual(
            [call.args[2] for call in operations._run_long_step.call_args_list],
            ["1" * 16, "2" * 16],
        )

    def test_file_transfer_commands_fit_the_serial_canonical_line(self) -> None:
        for name in ("baidu-home.json", "baidu-home.png"):
            with self.subTest(name=name):
                command = browse.evidence_frame_command("0123456789abcdef", name)
                self.assertLess(len((command + "\n").encode()), 768)

    def test_failure_diagnostics_are_compact_and_boundary_focused(self) -> None:
        commands = browse.browse_diagnostics_commands("0123456789abcdef")

        self.assertLessEqual(len(commands), 6)
        self.assertTrue(all(len((command + "\n").encode()) < 768 for command in commands))
        self.assertLess(sum(len(command.encode()) for command in commands), 2400)
        joined = "\n".join(commands)
        self.assertIn("dmesg --color=never", joined)
        self.assertIn("tail -c 131072", joined)
        self.assertIn("firefox-diagnostic-snapshot", joined)
        self.assertIn("baidu-home.json", joined)
        self.assertIn("systemctl show", joined)
        self.assertNotIn("/proc/net/tcp", joined)

    def test_real_failure_collection_uses_compact_browse_commands(self) -> None:
        operations = object.__new__(browse.RealFirefoxBrowseOperations)
        operations._collect_diagnostics_commands = mock.Mock(
            return_value=b"focused diagnostics\n"
        )

        with mock.patch.object(browse.secrets, "token_hex", return_value="1" * 16):
            result = operations.collect_diagnostics(45.0)

        self.assertEqual(result, b"focused diagnostics\n")
        operations._collect_diagnostics_commands.assert_called_once()
        timeout, nonce, commands = operations._collect_diagnostics_commands.call_args.args
        self.assertEqual(timeout, 45.0)
        self.assertEqual(nonce, "1" * 16)
        self.assertEqual(commands, browse.browse_diagnostics_commands("1" * 16))


if __name__ == "__main__":
    unittest.main()
