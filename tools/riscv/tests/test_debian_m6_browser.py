#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
)
from tools.riscv.debian.rootfs import desktop_m6_browser_gate
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DESKTOP_M6_JAVASCRIPT_STATUSES,
    DESKTOP_M6_REMOTE_MARKER,
    DesktopM6BrowserOperations,
    classify_desktop_m6_browser,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
)
from tools.riscv.debian.rootfs.profiles import get_profile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m6_browser_evidence.sh"
)
JAVASCRIPT_FIXTURE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m6_javascript.html"
)
JAVASCRIPT_PASS_FIXTURE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m6_javascript_pass.html"
)
ROOTFS_BUILDER = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
SESSION_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_session.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"


def _browser_transcript(status: str = "limited-pass") -> bytes:
    markers = (
        *DESKTOP_M5_QEMU_MILESTONES,
        *DESKTOP_M4_MILESTONES,
        DESKTOP_M6_REMOTE_MARKER,
        f"DEBIAN_BROWSER_M6_JAVASCRIPT status={status}",
        f"DEBIAN_BROWSER_M6_READY remote=baidu javascript={status}",
    )
    return ("\n".join(markers) + "\n").encode()


class DebianDesktopM6BrowserContractTests(unittest.TestCase):
    def test_m5_profile_adds_exact_browser_control_identity(self) -> None:
        profile = get_profile("desktop-m5-network")

        self.assertIn("xdotool", profile.requested_packages)
        self.assertEqual(profile.identity_packages[-2:], ("xdotool", "x11-apps"))
        self.assertEqual(
            DESKTOP_M6_REMOTE_MARKER,
            "DEBIAN_BROWSER_M6_REMOTE host=www.baidu.com "
            "resource=logo-png foreground=active",
        )

    def test_classifier_accepts_each_javascript_status(self) -> None:
        self.assertEqual(
            DESKTOP_M6_JAVASCRIPT_STATUSES,
            ("limited-pass", "disabled", "failed"),
        )
        for status in DESKTOP_M6_JAVASCRIPT_STATUSES:
            with self.subTest(status=status):
                result = classify_desktop_m6_browser(
                    _browser_transcript(status),
                    expected_debian_release="13.6",
                )
                self.assertTrue(result.passed, result.reason)

    def test_classifier_rejects_duplicate_reordered_and_mismatched_evidence(
        self,
    ) -> None:
        valid = _browser_transcript()
        javascript = b"DEBIAN_BROWSER_M6_JAVASCRIPT status=limited-pass\n"
        duplicate = valid.replace(javascript, javascript * 2)
        mismatch = valid.replace(
            b"DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
            b"DEBIAN_BROWSER_M6_READY remote=baidu javascript=failed",
        )
        remote = (DESKTOP_M6_REMOTE_MARKER + "\n").encode()
        reordered = remote + valid.replace(remote, b"")

        for transcript in (duplicate, mismatch, reordered):
            with self.subTest(transcript=transcript[-160:]):
                result = classify_desktop_m6_browser(
                    transcript,
                    expected_debian_release="13.6",
                )
                self.assertFalse(result.passed)

    def test_classifier_rejects_browser_failure_from_complete_transcript(self) -> None:
        result = classify_desktop_m6_browser(
            _browser_transcript()
            + b"DEBIAN_BROWSER_M6_FAIL reason=remote-title-timeout\n",
            expected_debian_release="13.6",
        )

        self.assertEqual(result.reason, "browser guest failure")


class DebianDesktopM6GuestEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_local_fixture_and_session_request_bounded_javascript(self) -> None:
        fixture = JAVASCRIPT_FIXTURE.read_text(encoding="utf-8")
        pass_fixture = JAVASCRIPT_PASS_FIXTURE.read_text(encoding="utf-8")
        session = SESSION_SCRIPT.read_text(encoding="utf-8")
        builder = ROOTFS_BUILDER.read_text(encoding="utf-8")

        self.assertIn("<title>ASTERINAS_JS_PENDING</title>", fixture)
        self.assertIn('id="javascript-status">ASTERINAS_JS_PENDING', fixture)
        self.assertIn(
            "window.location.replace('desktop-m6-javascript-pass.html')", fixture
        )
        self.assertIn("<title>ASTERINAS_JS_PASS</title>", pass_fixture)
        self.assertIn('id="javascript-status">ASTERINAS_JS_PASS', pass_fixture)
        self.assertIn("desktop_m6_javascript_pass.html", builder)
        self.assertIn("desktop-m6-javascript-pass.html", builder)
        self.assertIn("--enable_javascript=1", session)
        self.assertIn('netsurf-gtk "${browser_arguments[@]}" "$browser_url" &', session)
        self.assertIn("ASTERINAS_DESKTOP_BROWSER_VERBOSE", session)
        self.assertIn('"$HOME/netsurf-m7.log"', session)

    def _fake_environment(
        self,
        *,
        javascript_result: str,
        javascript_requested: bool = True,
        xdotool_mode: str = "normal",
    ) -> tuple[dict[str, str], Path, Path]:
        requested_label = "requested" if javascript_requested else "disabled"
        suffix = f"{javascript_result}-{requested_label}-{xdotool_mode}"
        fake_bin = self.directory / f"bin-{suffix}"
        fake_bin.mkdir()
        state = self.directory / f"state-{suffix}"
        typed = self.directory / f"typed-{suffix}"
        actions = self.directory / f"actions-{suffix}"
        console = self.directory / f"console-{suffix}"
        console.write_text("", encoding="utf-8")
        proc_root = self.directory / f"proc-{suffix}"
        process = proc_root / "777"
        process.mkdir(parents=True)
        arguments = b"/usr/bin/netsurf-gtk\0"
        if javascript_requested:
            arguments += b"--enable_javascript=1\0"
        (process / "cmdline").write_bytes(arguments)

        xdotool = fake_bin / "xdotool"
        xdotool.write_text(
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$ASTERINAS_M6_ACTIONS"
case "$1" in
  search)
    [ "$*" = 'search --classname ^netsurf-gtk$' ] || exit 10
    if [ "$ASTERINAS_M6_XDOTOOL_MODE" = duplicate ] || \
       [ "$ASTERINAS_M6_XDOTOOL_MODE" = nested ] || \
       [ "$ASTERINAS_M6_XDOTOOL_MODE" = auxiliary ]; then
      printf '42\n43\n'
    else
      printf '42\n'
    fi
    ;;
  getwindowpid)
    if [ "$ASTERINAS_M6_XDOTOOL_MODE" = nested ] && [ "$2" = 43 ]; then
      exit 1
    fi
    printf '777\n'
    ;;
  getwindowname)
    if [ "$ASTERINAS_M6_XDOTOOL_MODE" = auxiliary ] && [ "$2" = 43 ]; then
      printf 'NetSurf auxiliary\n'
    elif [ "$ASTERINAS_M6_XDOTOOL_MODE" = remote-pending ]; then
      printf 'NetSurf\n'
    elif [ "$ASTERINAS_M6_XDOTOOL_MODE" = oversized ]; then
      printf 'baidu%02050d\n' 0
    elif [ -f "$ASTERINAS_M6_STATE" ]; then
      case "$ASTERINAS_M6_JS_RESULT" in
        pass) printf 'ASTERINAS_JS_PASS\n' ;;
        pending) printf 'ASTERINAS_JS_PENDING\n' ;;
        *) exit 9 ;;
      esac
    else
      printf 'result.png - NetSurf\n'
    fi
    ;;
  set_desktop|set_desktop_for_window|windowmap|windowactivate) ;;
  type) printf '%s\n' "$*" >"$ASTERINAS_M6_TYPED" ;;
  key)
    [ "${2-}" != Return ] || : >"$ASTERINAS_M6_STATE"
    ;;
  *) exit 8 ;;
esac
""",
            encoding="utf-8",
        )
        pgrep = fake_bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nprintf '777\n'\n", encoding="utf-8")
        xdotool.chmod(0o755)
        pgrep.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_BROWSER_M6_CONSOLE=str(console),
            ASTERINAS_BROWSER_M6_PROC_ROOT=str(proc_root),
            ASTERINAS_BROWSER_M6_TIMEOUT_SECONDS="1",
            ASTERINAS_BROWSER_M6_COMMAND_TIMEOUT_SECONDS="1",
            ASTERINAS_BROWSER_M6_CAPTURE_DELAY_SECONDS="0",
            ASTERINAS_BROWSER_M6_POLL_DELAY_SECONDS="0",
            ASTERINAS_M6_JS_RESULT=javascript_result,
            ASTERINAS_M6_XDOTOOL_MODE=xdotool_mode,
            ASTERINAS_M6_STATE=str(state),
            ASTERINAS_M6_TYPED=str(typed),
            ASTERINAS_M6_ACTIONS=str(actions),
        )
        return environment, console, typed

    def test_guest_evidence_reports_limited_javascript_and_exact_local_url(
        self,
    ) -> None:
        environment, console, typed = self._fake_environment(javascript_result="pass")

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            [
                DESKTOP_M6_REMOTE_MARKER,
                "DEBIAN_BROWSER_M6_JAVASCRIPT status=limited-pass",
                "DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
            ],
        )
        self.assertIn(
            "file:///usr/share/asterinas/desktop-m6-javascript.html",
            typed.read_text(encoding="utf-8"),
        )
        actions = Path(environment["ASTERINAS_M6_ACTIONS"]).read_text(encoding="utf-8")
        self.assertIn("search --classname ^netsurf-gtk$\n", actions)
        self.assertIn("set_desktop_for_window 42 1\n", actions)
        self.assertLess(
            actions.index("set_desktop_for_window 42 1\n"),
            actions.index("set_desktop 1\n"),
        )
        self.assertNotIn("windowmap --sync 42\n", actions)

    def test_guest_evidence_distinguishes_failed_and_disabled_javascript(
        self,
    ) -> None:
        for requested, expected in ((True, "failed"), (False, "disabled")):
            with self.subTest(expected=expected):
                environment, console, _ = self._fake_environment(
                    javascript_result="pending",
                    javascript_requested=requested,
                )
                result = subprocess.run(
                    ["/bin/bash", str(EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    console.read_text(encoding="utf-8").splitlines()[-2:],
                    [
                        f"DEBIAN_BROWSER_M6_JAVASCRIPT status={expected}",
                        f"DEBIAN_BROWSER_M6_READY remote=baidu javascript={expected}",
                    ],
                )

    def test_guest_evidence_ignores_internal_window_without_process_identity(
        self,
    ) -> None:
        environment, console, _ = self._fake_environment(
            javascript_result="pass",
            xdotool_mode="nested",
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines()[-1],
            "DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
        )

    def test_guest_evidence_selects_remote_page_from_same_process_windows(
        self,
    ) -> None:
        environment, console, _ = self._fake_environment(
            javascript_result="pass",
            xdotool_mode="auxiliary",
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines()[-1],
            "DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
        )

    def test_guest_evidence_rejects_ambiguous_window_and_oversized_title(
        self,
    ) -> None:
        for mode, reason in (
            ("duplicate", "ambiguous-window"),
            ("oversized", "window-title-too-long"),
        ):
            with self.subTest(mode=mode):
                environment, console, _ = self._fake_environment(
                    javascript_result="pass",
                    xdotool_mode=mode,
                )
                result = subprocess.run(
                    ["/bin/bash", str(EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    console.read_text(encoding="utf-8").splitlines(),
                    [f"DEBIAN_BROWSER_M6_FAIL reason={reason}"],
                )

    def test_guest_evidence_hex_encodes_last_title_before_remote_timeout(
        self,
    ) -> None:
        environment, console, _ = self._fake_environment(
            javascript_result="pass",
            xdotool_mode="remote-pending",
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            [
                "DEBIAN_BROWSER_M6_DIAGNOSTIC title_hex=4e657453757266",
                "DEBIAN_BROWSER_M6_FAIL reason=remote-title-timeout",
            ],
        )


class DebianDesktopM6AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _operations(self) -> DesktopM6BrowserOperations:
        operations = object.__new__(DesktopM6BrowserOperations)
        operations._javascript_screenshot = b""
        operations._javascript_screenshot_metadata = {}
        operations._javascript_status = ""
        return operations

    def test_adapter_reuses_m5_identity_network_and_remote_stop_marker(self) -> None:
        self.assertEqual(DesktopM6BrowserOperations.SCHEMA_VERSION, 5)
        self.assertEqual(DesktopM6BrowserOperations.PROFILE_NAME, "desktop-m5-network")
        self.assertEqual(
            DesktopM6BrowserOperations.ARTIFACT_PREFIX, "desktop-m6-browser"
        )
        self.assertEqual(
            DesktopM6BrowserOperations.MILESTONES[-1], DESKTOP_M6_REMOTE_MARKER
        )
        self.assertIs(DesktopM6BrowserOperations._qemu_argv, desktop_m5_qemu_argv)

    def test_protocol_captures_javascript_after_inherited_remote_screenshot(
        self,
    ) -> None:
        operations = self._operations()
        events: list[object] = []

        class Serial:
            def wait_for_any(
                self, markers: tuple[bytes, ...], deadline: float
            ) -> bytes:
                del deadline
                events.append(("javascript", markers))
                return b"DEBIAN_BROWSER_M6_JAVASCRIPT status=limited-pass"

            def wait_for(self, marker: bytes, deadline: float) -> bytes:
                del deadline
                events.append(("ready", marker))
                return marker

        session = {
            "serial": Serial(),
            "monitor": object(),
            "directory": self.directory,
        }
        config = SimpleNamespace(boot_timeout=2.0, command_timeout=1.0)
        metadata = {
            "width": 1280,
            "height": 1024,
            "pixel_count": 1280 * 1024,
            "distinct_sampled_colors": 4,
            "non_background_pixels": 1000,
        }

        with (
            mock.patch.object(
                DesktopM5QemuOperations,
                "run_protocol",
                autospec=True,
                side_effect=lambda *_: events.append("remote-screenshot"),
            ),
            mock.patch.object(
                desktop_m6_browser_gate,
                "capture_rendered_ppm",
                return_value=(b"javascript-ppm", metadata),
            ) as capture,
        ):
            operations.run_protocol(session, config)

        self.assertEqual(events[0], "remote-screenshot")
        self.assertEqual(events[1][0], "javascript")
        self.assertEqual(
            events[2],
            (
                "ready",
                b"DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
            ),
        )
        self.assertEqual(operations._javascript_status, "limited-pass")
        self.assertEqual(operations._javascript_screenshot, b"javascript-ppm")
        capture.assert_called_once_with(
            session["monitor"],
            self.directory / "desktop-m6-javascript.ppm",
            mock.ANY,
        )

    def test_protocol_preserves_failure_frame_without_claiming_remote_evidence(
        self,
    ) -> None:
        operations = self._operations()
        operations._remote_evidence = False
        session = {
            "serial": object(),
            "monitor": object(),
            "directory": self.directory,
        }
        config = SimpleNamespace(boot_timeout=2.0, command_timeout=1.0)
        metadata = {"width": 1280, "height": 1024}

        with (
            mock.patch.object(
                DesktopM5QemuOperations,
                "run_protocol",
                autospec=True,
                side_effect=desktop_m6_browser_gate.GateFailure(
                    "guest reported desktop failure"
                ),
            ),
            mock.patch.object(
                desktop_m6_browser_gate,
                "capture_rendered_ppm",
                return_value=(b"failure-ppm", metadata),
            ),
            self.assertRaises(desktop_m6_browser_gate.GateFailure),
        ):
            operations.run_protocol(session, config)

        self.assertFalse(operations._remote_evidence)
        self.assertEqual(operations._screenshot, b"failure-ppm")
        self.assertEqual(operations._screenshot_metadata, metadata)

    def test_publish_records_status_and_both_screenshot_metadata(self) -> None:
        operations = self._operations()
        operations._javascript_status = "failed"
        operations._javascript_screenshot = b"js-ppm"
        operations._javascript_screenshot_metadata = {"width": 1280, "height": 1024}
        operations._remote_evidence = True
        result: dict[str, object] = {}
        writes: list[tuple[str, bytes, int]] = []

        class Output:
            def atomic_write(self, name: str, payload: bytes, *, mode: int) -> None:
                writes.append((name, payload, mode))

        operations._require_config = lambda config: None
        operations._require_output = lambda: Output()
        with mock.patch.object(
            DesktopM5QemuOperations,
            "publish",
            autospec=True,
        ) as inherited:
            operations.publish(object(), object(), b"transcript", result)

        self.assertEqual(result["javascript_status"], "failed")
        self.assertTrue(result["remote_evidence"])
        self.assertEqual(
            result["javascript_screenshot"],
            {"width": 1280, "height": 1024},
        )
        self.assertEqual(
            writes,
            [("desktop-m6-javascript.ppm", b"js-ppm", 0o600)],
        )
        inherited.assert_called_once()

    def test_make_target_uses_distinct_output_and_cold_boot_budget(self) -> None:
        target = (
            MAKEFILE.read_text(encoding="utf-8")
            .split(".PHONY: test_riscv_debian_desktop_m6_browser_gate", 1)[1]
            .split(".PHONY:", 1)[0]
        )

        self.assertIn("DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT", target)
        self.assertIn("desktop_m6_browser_gate", target)
        self.assertIn('--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', target)


if __name__ == "__main__":
    unittest.main()
