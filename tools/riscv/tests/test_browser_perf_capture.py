#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-session, non-reboot capture tests for the Firefox timing fixture."""

from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from tools.riscv.debian.rootfs.browser_perf_capture import (
    _wait_document,
    CaptureError,
    capture_local,
    performance_urls,
    fixture_index_url_from_environment,
    main,
    resolve_fixture_index_url,
    run_capture,
)


BASE = "http://10.0.2.2:17894/browser-quality/index.html"
PHYSICAL_BASE = "http://10.100.19.216:17894/browser-quality/index.html"
NAVIGATION = {
    "schemaVersion": 1,
    "clockDomain": "browser-navigation",
    "startTime": 0,
    "fetchStart": 10,
    "responseStart": 20,
    "responseEnd": 30,
    "domContentLoadedEventEnd": 40,
    "loadEventEnd": 50,
}


class FakeMarionette:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.timeout_resets: list[float] = []
        self.url = "about:blank"

    def set_timeout(self, timeout: float) -> None:
        self.timeout_resets.append(timeout)

    def close(self) -> None:
        pass

    def command(self, name: str, parameters: object | None = None) -> object:
        self.commands.append(name)
        if name == "WebDriver:Navigate":
            assert isinstance(parameters, dict)
            self.url = str(parameters["url"])
            return {"value": None}
        if name == "WebDriver:NewSession":
            return {
                "value": {
                    "sessionId": "perf-session",
                    "capabilities": {"acceptInsecureCerts": False},
                }
            }
        if name == "WebDriver:GetWindowHandles":
            return {"value": ["window-1"]}
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            script = str(parameters["script"])
            if "__asterinasRunSyntheticTiming" in script:
                return {"value": "started"}
            if "__asterinasTimingSnapshot" in script:
                samples = [
                    {
                        "kind": kind,
                        "source": "synthetic",
                        "firstRafMs": 2,
                        "nextRafMs": 4,
                    }
                    for kind in ("keyboard", "pointer", "scroll")
                ]
                return {
                    "value": json.dumps(
                        {
                            "schemaVersion": 1,
                            "clockDomain": "browser-performance-now",
                            "samples": samples,
                        }
                    )
                }
            if "__asterinasNavigationSnapshot" in script:
                return {"value": json.dumps(NAVIGATION)}
            if "document.readyState" in script:
                return {
                    "value": json.dumps({"url": self.url, "readyState": "complete"})
                }
        raise AssertionError(f"unexpected command {name}")


class SessionBudgetMarionette(FakeMarionette):
    """Observe the command's deadline reset, not a mocked socket timer."""

    def __init__(self) -> None:
        super().__init__()
        self.new_session_budget: list[float] | None = None

    def command(self, name: str, parameters: object | None = None) -> object:
        if name == "WebDriver:NewSession":
            self.new_session_budget = self.timeout_resets.copy()
        return super().command(name, parameters)


class XrayMarionette(FakeMarionette):
    """Model Firefox's documented hiding of page-defined window properties."""

    def command(self, name: str, parameters: object | None = None) -> object:
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            script = str(parameters["script"])
            for page_function in (
                "__asterinasRunSyntheticTiming",
                "__asterinasTimingSnapshot",
                "__asterinasNavigationSnapshot",
            ):
                if page_function in script and (
                    f"window.wrappedJSObject.{page_function}" not in script
                ):
                    return {
                        "value": "missing"
                        if page_function == "__asterinasRunSyntheticTiming"
                        else "null"
                    }
        return super().command(name, parameters)


class TransientDocumentMarionette(FakeMarionette):
    """A new browsing context can briefly return null to ExecuteScript."""

    def __init__(self) -> None:
        super().__init__()
        self.returned_null = False

    def command(self, name: str, parameters: object | None = None) -> object:
        if name == "WebDriver:ExecuteScript" and self.url.endswith("/perf-second.html"):
            assert isinstance(parameters, dict)
            if (
                "document.readyState" in str(parameters["script"])
                and not self.returned_null
            ):
                self.returned_null = True
                return {"value": None}
        return super().command(name, parameters)


class UnexpectedDocumentMarionette(FakeMarionette):
    def __init__(self) -> None:
        super().__init__()
        self.readiness_calls = 0

    def command(self, name: str, parameters: object | None = None) -> object:
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            if "document.readyState" in str(parameters["script"]):
                self.readiness_calls += 1
                if self.readiness_calls == 1:
                    return {
                        "value": json.dumps(
                            {
                                "url": "about:neterror?secret=do-not-log",
                                "readyState": "complete",
                            }
                        )
                    }
                raise TimeoutError("Marionette gate deadline expired")
        return super().command(name, parameters)


class MalformedStateMarionette(UnexpectedDocumentMarionette):
    def command(self, name: str, parameters: object | None = None) -> object:
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            if "document.readyState" in str(parameters["script"]):
                self.readiness_calls += 1
                if self.readiness_calls == 1:
                    return {
                        "value": json.dumps(
                            {
                                "url": "about:neterror?secret=do-not-log",
                                "readyState": [],
                            }
                        )
                    }
                raise TimeoutError("Marionette gate deadline expired")
        return super().command(name, parameters)


class InvalidNavigationMarionette(FakeMarionette):
    def __init__(self) -> None:
        super().__init__()
        self.navigation_calls = 0

    def command(self, name: str, parameters: object | None = None) -> object:
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            if "__asterinasNavigationSnapshot" in str(parameters["script"]):
                self.navigation_calls += 1
                if self.navigation_calls == 1:
                    return {"value": json.dumps({**NAVIGATION, "loadEventEnd": 0})}
                raise TimeoutError("Marionette gate deadline expired")
        return super().command(name, parameters)


class BrowserPerfCaptureTests(unittest.TestCase):
    def test_performance_urls_require_frozen_local_fixture_origin(self) -> None:
        self.assertEqual(
            performance_urls(BASE),
            (
                "http://10.0.2.2:17894/browser-quality/perf.html",
                "http://10.0.2.2:17894/browser-quality/perf-second.html",
            ),
        )
        self.assertEqual(
            performance_urls(PHYSICAL_BASE),
            (
                "http://10.100.19.216:17894/browser-quality/perf.html",
                "http://10.100.19.216:17894/browser-quality/perf-second.html",
            ),
        )
        for raw in (
            "https://10.0.2.2:17894/browser-quality/index.html",
            "http://10.0.2.2:17895/browser-quality/index.html",
            "http://10.0.2.2:17894/browser-quality/index.html?x=1",
            "http://user@10.0.2.2:17894/browser-quality/index.html",
            "http://localhost:17894/browser-quality/index.html",
        ):
            with self.subTest(raw=raw), self.assertRaises(CaptureError):
                performance_urls(raw)

    def test_environment_accepts_only_qemu_or_board_fixture_endpoint(self) -> None:
        for host in ("10.0.2.2", "10.100.19.216"):
            with (
                self.subTest(host=host),
                mock.patch.dict(
                    "os.environ",
                    {
                        "ASTERINAS_DESKTOP_FIXTURE_URL": f"http://{host}:17894/asterinas-network-probe.bin"
                    },
                ),
            ):
                self.assertEqual(
                    fixture_index_url_from_environment(),
                    f"http://{host}:17894/browser-quality/index.html",
                )
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "ASTERINAS_DESKTOP_FIXTURE_URL": "http://10.100.19.217:17894/asterinas-network-probe.bin"
                },
            ),
            self.assertRaises(CaptureError),
        ):
            fixture_index_url_from_environment()

    def test_explicit_fixture_index_url_works_from_isolated_root_console(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_fixture_index_url(PHYSICAL_BASE), PHYSICAL_BASE)
            with self.assertRaises(CaptureError):
                resolve_fixture_index_url(None)

    def test_cli_reports_the_specific_contract_failure_without_a_secret_url(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                "os.environ",
                {"ASTERINAS_DESKTOP_FIXTURE_URL": "http://wrong.example:17894/secret"},
                clear=True,
            ),
            mock.patch(
                "sys.argv",
                [
                    "browser_perf_capture.py",
                    "--firefox-pid",
                    "116",
                    "--xorg-pid",
                    "75",
                    "--evidence-dir",
                    str(Path(directory) / "evidence"),
                ],
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as error_output,
        ):
            self.assertEqual(main(), 1)
            message = error_output.getvalue()
        self.assertIn(
            'reason=CaptureError detail="desktop fixture URL is outside the frozen contract"',
            message,
        )
        self.assertNotIn("wrong.example", message)
        self.assertNotIn("/secret", message)

    def test_cli_uses_bounded_retrying_marionette_connection(self) -> None:
        client = FakeMarionette()
        report = {"navigation_parts": {"total_ms": 50}}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_perf_capture._connect",
                return_value=client,
            ) as connect,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_perf_capture.run_capture",
                return_value=report,
            ),
            mock.patch(
                "sys.argv",
                [
                    "browser_perf_capture.py",
                    "--firefox-pid",
                    "116",
                    "--xorg-pid",
                    "75",
                    "--timeout-seconds",
                    "5",
                    "--evidence-dir",
                    str(Path(directory) / "evidence"),
                    "--fixture-index-url",
                    PHYSICAL_BASE,
                ],
            ),
        ):
            before = time.monotonic()
            self.assertEqual(main(), 0)
            after = time.monotonic()
        connect.assert_called_once()
        host, port, deadline = connect.call_args.args
        self.assertEqual((host, port), ("127.0.0.1", 2828))
        self.assertGreaterEqual(deadline, before + 5)
        self.assertLessEqual(deadline, after + 5)

    def test_local_capture_collects_synthetic_frames_and_complete_navigation(
        self,
    ) -> None:
        client = FakeMarionette()
        report = capture_local(client, BASE, synthetic_samples=1, timeout_seconds=5)
        self.assertEqual(report["interaction_summary"]["source"], "synthetic")
        self.assertEqual(report["navigation_parts"]["total_ms"], 50)
        self.assertEqual(report["navigation_parts"]["request_to_first_byte_ms"], 10)
        self.assertEqual(client.commands.count("WebDriver:Navigate"), 2)
        self.assertNotIn("WebDriver:DeleteSession", client.commands)
        self.assertNotIn("Reboot", " ".join(client.commands))

    def test_local_capture_reads_page_functions_through_firefox_xray(self) -> None:
        try:
            report = capture_local(
                XrayMarionette(), BASE, synthetic_samples=1, timeout_seconds=5
            )
        except CaptureError as error:
            self.fail(f"Firefox Xray rejected local timing: {error}")
        self.assertEqual(report["interaction_summary"]["source"], "synthetic")
        self.assertEqual(report["navigation_parts"]["total_ms"], 50)

    def test_local_capture_retries_transient_null_only_during_document_wait(
        self,
    ) -> None:
        client = TransientDocumentMarionette()
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_perf_capture.time.sleep"
        ) as pause:
            try:
                report = capture_local(
                    client, BASE, synthetic_samples=1, timeout_seconds=5
                )
            except CaptureError as error:
                self.fail(f"new Firefox browsing context was rejected: {error}")
        self.assertTrue(client.returned_null)
        pause.assert_called_once_with(0.25)
        self.assertEqual(report["navigation_parts"]["total_ms"], 50)

    def test_document_wait_reports_redacted_last_state_on_transport_timeout(
        self,
    ) -> None:
        client = UnexpectedDocumentMarionette()
        try:
            _wait_document(
                client,
                "http://10.0.2.2:17894/browser-quality/perf-second.html",
                time.monotonic() + 1,
            )
        except CaptureError as error:
            message = str(error)
            self.assertIn("observed=about:complete", message)
            self.assertNotIn("secret", message)
        except TimeoutError as error:
            self.fail(f"document stage was lost on transport timeout: {error}")
        else:
            self.fail("the wrong document was accepted")

    def test_document_wait_rejects_unhashable_ready_state_without_url_leak(
        self,
    ) -> None:
        try:
            _wait_document(
                MalformedStateMarionette(),
                "http://10.0.2.2:17894/browser-quality/perf-second.html",
                time.monotonic() + 1,
            )
        except CaptureError as error:
            self.assertIn("observed=about:unknown", str(error))
            self.assertNotIn("secret", str(error))
        except TypeError as error:
            self.fail(f"malformed readyState escaped document validation: {error}")
        else:
            self.fail("malformed readyState was accepted")

    def test_navigation_timeout_keeps_last_validation_error_and_low_poll_rate(
        self,
    ) -> None:
        client = InvalidNavigationMarionette()
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_perf_capture.time.sleep"
        ) as pause:
            try:
                capture_local(client, BASE, synthetic_samples=1, timeout_seconds=5)
            except CaptureError as error:
                self.assertIn("navigation is incomplete or reordered", str(error))
            except TimeoutError as error:
                self.fail(f"navigation validation was lost on timeout: {error}")
            else:
                self.fail("incomplete navigation was accepted")
        pause.assert_called_once_with(0.25)

    def test_opt_in_navigation_diagnostic_emits_only_one_numeric_summary(self) -> None:
        client = InvalidNavigationMarionette()
        with (
            mock.patch.dict("os.environ", {"ASTERINAS_BROWSER_PERF_DIAGNOSTICS": "1"}),
            mock.patch("tools.riscv.debian.rootfs.browser_perf_capture.time.sleep"),
            mock.patch("sys.stderr", new_callable=io.StringIO) as error_output,
        ):
            with self.assertRaises(CaptureError):
                capture_local(client, BASE, synthetic_samples=1, timeout_seconds=5)
            diagnostic = error_output.getvalue()
        self.assertEqual(diagnostic.count("A_BROWSER_PERF_NAVIGATION_DIAGNOSTIC "), 1)
        self.assertIn('"loadEventEnd":0', diagnostic)
        self.assertNotIn("browser-quality", diagnostic)
        self.assertNotIn("10.0.2.2", diagnostic)

    def test_opt_in_interaction_checkpoint_survives_navigation_rejection(self) -> None:
        client = InvalidNavigationMarionette()
        with (
            mock.patch.dict("os.environ", {"ASTERINAS_BROWSER_PERF_DIAGNOSTICS": "1"}),
            mock.patch("tools.riscv.debian.rootfs.browser_perf_capture.time.sleep"),
            mock.patch("sys.stderr", new_callable=io.StringIO) as error_output,
        ):
            with self.assertRaises(CaptureError):
                capture_local(client, BASE, synthetic_samples=1, timeout_seconds=5)
        diagnostic = error_output.getvalue()
        prefix = "A_BROWSER_PERF_INTERACTION_DIAGNOSTIC "
        self.assertEqual(diagnostic.count(prefix), 1)
        checkpoint = json.loads(diagnostic.split(prefix, 1)[1].splitlines()[0])
        self.assertEqual(checkpoint["source"], "synthetic")
        self.assertEqual(checkpoint["keyboard"]["next_raf_ms"]["count"], 1)
        self.assertEqual(checkpoint["pointer"]["next_raf_ms"]["p95_ms"], 4)
        self.assertNotIn("10.0.2.2", diagnostic)
        self.assertNotIn("browser-quality", diagnostic)

    def test_capture_persists_interaction_checkpoint_before_navigation_rejection(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("tools.riscv.debian.rootfs.browser_perf_capture.time.sleep"),
        ):
            output = Path(directory)
            with self.assertRaises(CaptureError):
                run_capture(
                    InvalidNavigationMarionette(),
                    BASE,
                    firefox_pid=116,
                    xorg_pid=75,
                    evidence_dir=output,
                    synthetic_samples=1,
                    timeout_seconds=5,
                    cpu_sample_fn=lambda path, _pids: path.write_text("{}"),
                    identity_fn=lambda _pids: (100, 200),
                )
            checkpoint = json.loads(
                (output / "browser-interaction-capture.json").read_text()
            )
            self.assertEqual(checkpoint["schema_version"], 1)
            self.assertEqual(checkpoint["interaction_summary"]["source"], "synthetic")
            self.assertEqual(
                checkpoint["interaction_summary"]["keyboard"]["next_raf_ms"]["count"],
                1,
            )
            self.assertTrue((output / "browser-system-time.json").is_file())
            self.assertFalse((output / "browser-local-capture.json").exists())

    def test_local_capture_rejects_bad_sample_bound_before_browser_commands(
        self,
    ) -> None:
        client = FakeMarionette()
        for value in (0, 17, True):
            with self.subTest(value=value), self.assertRaises(CaptureError):
                capture_local(client, BASE, synthetic_samples=value, timeout_seconds=5)
        self.assertEqual(client.commands, [])

    def test_capture_publishes_private_local_and_cpu_artifacts_without_session_delete(
        self,
    ) -> None:
        client = FakeMarionette()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = run_capture(
                client,
                BASE,
                firefox_pid=116,
                xorg_pid=75,
                evidence_dir=output,
                synthetic_samples=1,
                timeout_seconds=5,
                cpu_sample_fn=lambda path, pids: path.write_text(
                    json.dumps({"process_ids": list(pids), "intervals": []})
                ),
                identity_fn=lambda _pids: (100, 200),
            )
            local = output / "browser-local-capture.json"
            cpu = output / "browser-system-time.json"
            self.assertEqual(result["navigation_parts"]["total_ms"], 50)
            self.assertEqual(json.loads(local.read_text())["schema_version"], 1)
            self.assertEqual(json.loads(cpu.read_text())["process_ids"], [116, 75])
            self.assertEqual(stat.S_IMODE(local.stat().st_mode), 0o600)
            self.assertEqual(client.timeout_resets, [5, 5])
            with self.assertRaises(CaptureError):
                run_capture(
                    client,
                    BASE,
                    firefox_pid=116,
                    xorg_pid=75,
                    evidence_dir=output,
                    synthetic_samples=1,
                    timeout_seconds=5,
                    cpu_sample_fn=lambda _path, _pids: None,
                    identity_fn=lambda _pids: (100, 200),
                )
        self.assertNotIn("WebDriver:DeleteSession", client.commands)

    def test_new_session_gets_a_fresh_bounded_budget_after_greeting(self) -> None:
        client = SessionBudgetMarionette()
        with tempfile.TemporaryDirectory() as directory:
            run_capture(
                client,
                BASE,
                firefox_pid=116,
                xorg_pid=75,
                evidence_dir=Path(directory),
                synthetic_samples=1,
                timeout_seconds=5,
                cpu_sample_fn=lambda path, _pids: path.write_text("{}"),
                identity_fn=lambda _pids: (100, 200),
            )
        self.assertEqual(client.new_session_budget, [5])
        self.assertEqual(client.timeout_resets, [5, 5])

    def test_capture_rejects_pid_aliases_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CaptureError):
                run_capture(
                    FakeMarionette(),
                    BASE,
                    firefox_pid=116,
                    xorg_pid=116,
                    evidence_dir=Path(directory),
                    synthetic_samples=1,
                    timeout_seconds=5,
                    cpu_sample_fn=lambda _path, _pids: None,
                    identity_fn=lambda _pids: (100, 200),
                )

    def test_capture_rejects_process_replacement_after_sample_interval(self) -> None:
        identities = iter(((100, 200), (101, 200)))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(CaptureError, "identity"):
                run_capture(
                    FakeMarionette(),
                    BASE,
                    firefox_pid=116,
                    xorg_pid=75,
                    evidence_dir=output,
                    synthetic_samples=1,
                    timeout_seconds=5,
                    cpu_sample_fn=lambda path, _pids: path.write_text("{}"),
                    identity_fn=lambda _pids: next(identities),
                )
            self.assertFalse((output / "browser-local-capture.json").exists())


if __name__ == "__main__":
    unittest.main()
