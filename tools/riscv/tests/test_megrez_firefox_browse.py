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
        self.ready = False

    def start(self) -> None:
        self.events.append("proxy-start")
        if self.fail:
            raise RuntimeError("proxy unavailable")
        self.ready = True

    def close(self) -> None:
        self.events.append("proxy-close")

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "upstream": "127.0.0.1:7890",
            "ready": self.ready,
        }


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

    def synchronize_clock(self, _timeout: float) -> dict[str, object]:
        self.events.append("clock")
        return {
            "marker": "ASTERINAS_CLOCK_SYNC_READY",
            "guest_unix_seconds": 1789099506,
            "host_unix_seconds": 1789099506,
            "source": "host-serial",
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
            "screenshot_height": 887,
            "screenshot_sha256": hashlib.sha256(_png()).hexdigest(),
            "screenshot_source": "framebuffer",
            "screenshot_width": 1280,
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
    def test_framebuffer_marker_is_bound_to_transferred_png(self) -> None:
        payload = _png()
        marker = {
            "screenshot_height": 1080,
            "screenshot_sha256": hashlib.sha256(payload).hexdigest(),
            "screenshot_source": "framebuffer",
            "screenshot_width": 1920,
        }
        browse._validated_framebuffer_screenshot(
            payload, marker, width=1920, height=1080
        )
        for name, value in (
            ("screenshot_height", 1079),
            ("screenshot_sha256", "0" * 64),
            ("screenshot_source", "marionette"),
            ("screenshot_width", 1919),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                browse.HostGateError, "framebuffer screenshot marker"
            ):
                browse._validated_framebuffer_screenshot(
                    payload, {**marker, name: value}, width=1920, height=1080
                )

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
                (
                    b'{"guest_unix_seconds":1789099507,'
                    b'"host_unix_seconds":1789099506,'
                    b'"marker":"ASTERINAS_CLOCK_SYNC_READY",'
                    b'"source":"host-serial"}\n'
                ),
                (
                    b'{"marker":"DEBIAN_BROWSER_WEB_BAIDU_HOME_READY",'
                    b'"scope":"baidu-home","title_sha256":"'
                    + b"0" * 64
                    + b'","tls":"verified","url":"https://www.baidu.com/"}\n'
                ),
            )
        )

        with mock.patch.object(browse.time, "time", return_value=1789099506.75):
            clock = operations.synchronize_clock(45)
        home = operations.run_baidu_home(
            116,
            "0123456789abcdef",
            browse.FirefoxBrowseConfig().browse_timeout,
        )

        self.assertEqual(clock["marker"], "ASTERINAS_CLOCK_SYNC_READY")
        self.assertEqual(home["scope"], "baidu-home")
        clock_command = operations._run_long_step.call_args_list[0].args[0]
        home_command = operations._run_long_step.call_args_list[1].args[0]
        self.assertNotIn(
            "accepted_statuses",
            operations._run_long_step.call_args_list[0].kwargs,
        )
        self.assertNotIn(
            "accepted_statuses",
            operations._run_long_step.call_args_list[1].kwargs,
        )
        self.assertIn("date --utc --set @$_r", clock_command)
        self.assertIn('"source":"host-serial"', clock_command)
        self.assertNotIn("megrez-clock-sync", clock_command)
        self.assertNotIn("nsenter", clock_command)
        self.assertLess(len((clock_command + "\n").encode()), 768)
        self.assertIn("browser-web-marionette-gate --scope baidu-home", home_command)
        self.assertIn("--screenshot-backend framebuffer", home_command)
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

    def test_baidu_gate_buffers_retry_noise_and_replays_a_bounded_summary(self) -> None:
        operations = object.__new__(browse.RealFirefoxBrowseOperations)
        serial = mock.Mock()
        serial.checkpoint.return_value = 10
        operations._require_serial = mock.Mock(return_value=serial)
        operations._run_long_step = mock.Mock()
        operations._step_payload = mock.Mock(
            return_value=(
                b'{"marker":"DEBIAN_BROWSER_WEB_BAIDU_HOME_READY",'
                b'"scope":"baidu-home"}\n'
            )
        )

        operations.run_baidu_home(
            116,
            "0123456789abcdef",
            browse.FirefoxBrowseConfig().browse_timeout,
        )

        command = operations._run_long_step.call_args.args[0]
        self.assertEqual(
            getattr(browse, "MAX_GATE_DIAGNOSTIC_LINES", None),
            64,
        )
        self.assertIn("l=$d/g.log", command)
        self.assertIn("2>$l", command)
        self.assertIn("A_WEB_CONNECT_RETRIES count=%s", command)
        self.assertIn(
            "/usr/bin/grep -c 'phase=tcp-connect state=exception' $l",
            command,
        )
        self.assertIn("/usr/bin/grep -v 'phase=tcp-connect'", command)
        self.assertIn("/usr/bin/tail -n 64", command)
        self.assertTrue(command.endswith('(exit "$q")'))
        self.assertLess(len((command + "\n").encode()), 768)

    def test_clock_sync_rejects_guest_time_outside_the_serial_attestation(self) -> None:
        operations = object.__new__(browse.RealFirefoxBrowseOperations)
        serial = mock.Mock()
        serial.checkpoint.return_value = 10
        operations._require_serial = mock.Mock(return_value=serial)
        operations._run_long_step = mock.Mock()
        operations._step_payload = mock.Mock(
            return_value=(
                b'{"guest_unix_seconds":1789099512,'
                b'"host_unix_seconds":1789099506,'
                b'"marker":"ASTERINAS_CLOCK_SYNC_READY",'
                b'"source":"host-serial"}\n'
            )
        )

        with mock.patch.object(browse.time, "time", return_value=1789099506.75):
            with self.assertRaisesRegex(
                browse.HostGateError, "guest clock serial evidence is invalid"
            ):
                operations.synchronize_clock(45)

    def test_clock_sync_rejects_an_out_of_range_host_epoch_before_serial(self) -> None:
        for host_epoch in (1704067199.9, 4133980800.0):
            with self.subTest(host_epoch=host_epoch):
                operations = object.__new__(browse.RealFirefoxBrowseOperations)
                serial = mock.Mock()
                serial.checkpoint.return_value = 10
                operations._require_serial = mock.Mock(return_value=serial)
                operations._run_long_step = mock.Mock()
                operations._step_payload = mock.Mock(
                    return_value=(
                        b'{"guest_unix_seconds":1704067200,'
                        b'"host_unix_seconds":1704067200,'
                        b'"marker":"ASTERINAS_CLOCK_SYNC_READY",'
                        b'"source":"host-serial"}\n'
                    )
                )

                with mock.patch.object(browse.time, "time", return_value=host_epoch):
                    with self.assertRaisesRegex(
                        browse.HostGateError, "host clock is outside"
                    ):
                        operations.synchronize_clock(45)

                operations._run_long_step.assert_not_called()

    def test_passing_result_requires_latched_proxy_readiness(self) -> None:
        values = {
            "schema_version": 1,
            "passed": True,
            "physical": True,
            "reason": "baidu-home-ready",
            "failure": "",
            "plan_sha256": "a" * 64,
            "bootargs_sha256": "b" * 64,
            "recovered": True,
            "physical_boots": 1,
            "readiness": BootReadinessEvidence(
                browser_pid=116,
                framebuffer=True,
                xorg_fbdev=True,
                openbox=True,
                firefox=True,
                browser_service="active",
                browser_restarts=0,
            ),
            "clock_evidence": {"marker": "ASTERINAS_CLOCK_SYNC_READY"},
            "page_marker": {"marker": "DEBIAN_BROWSER_WEB_BAIDU_HOME_READY"},
            "transport": ("kernel:mmc",),
            "serial_sha256": "c" * 64,
            "diagnostics_sha256": "d" * 64,
            "page_json_sha256": "e" * 64,
            "screenshot_sha256": "f" * 64,
            "total_seconds": 1.0,
        }
        for proxy_summary in ({"schema_version": 1}, {"ready": False}):
            with self.subTest(proxy_summary=proxy_summary), self.assertRaisesRegex(
                browse.HostGateError, "passing Firefox browse result is incomplete"
            ):
                browse.FirefoxBrowseResult(
                    **values,
                    proxy_bridge=proxy_summary,
                )

        failed = browse.FirefoxBrowseResult(
            **{
                **values,
                "passed": False,
                "reason": "baidu-home-incomplete",
                "failure": "proxy-start-failed",
                "proxy_bridge": {"schema_version": 1, "ready": False},
            }
        )
        self.assertFalse(failed.passed)

    def test_file_transfer_commands_fit_the_serial_canonical_line(self) -> None:
        for name in ("baidu-home.json", "baidu-home.png"):
            with self.subTest(name=name):
                command = browse.evidence_frame_command("0123456789abcdef", name)
                self.assertLess(len((command + "\n").encode()), 768)

    def test_failure_diagnostics_are_compact_and_boundary_focused(self) -> None:
        commands = browse.browse_diagnostics_commands("0123456789abcdef")

        self.assertLessEqual(len(commands), 6)
        # Non-final commands receive the acknowledged-shell wrapper before
        # the 768-byte serial limit is enforced.
        self.assertTrue(
            all(len(command.encode()) < 480 for command in commands[:-1])
        )
        self.assertLess(len((commands[-1] + "\n").encode()), 768)
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
