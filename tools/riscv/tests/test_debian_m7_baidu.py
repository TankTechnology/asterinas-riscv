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

from tools.riscv.debian.rootfs import desktop_m7_baidu_gate
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DesktopM6BrowserOperations,
)
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import (
    DESKTOP_M7_HOME_MARKER,
    DESKTOP_M7_READY_MARKER,
    DESKTOP_M7_SEARCH_MARKER,
    DesktopM7BaiduOperations,
    classify_desktop_m7_baidu,
)
from tools.riscv.tests.test_debian_m6_browser import _browser_transcript


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m7_baidu_evidence.sh"
)
ROOTFS_BUILDER = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"


def _m7_transcript() -> bytes:
    return (
        _browser_transcript()
        + (
            "\n".join(
                (
                    DESKTOP_M7_HOME_MARKER,
                    DESKTOP_M7_SEARCH_MARKER,
                    DESKTOP_M7_READY_MARKER,
                )
            )
            + "\n"
        ).encode()
    )


class DebianDesktopM7BaiduContractTests(unittest.TestCase):
    def test_classifier_accepts_exact_ordered_homepage_and_search(self) -> None:
        result = classify_desktop_m7_baidu(
            _m7_transcript(), expected_debian_release="13.6"
        )

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(
            DESKTOP_M7_HOME_MARKER,
            "DEBIAN_BROWSER_M7_HOME url=https://m.baidu.com/ "
            "variant=mobile title=baidu process=netsurf",
        )
        self.assertEqual(
            DESKTOP_M7_SEARCH_MARKER,
            "DEBIAN_BROWSER_M7_SEARCH query=asterinas result=loaded",
        )

    def test_classifier_rejects_missing_duplicate_reordered_and_failure(self) -> None:
        valid = _m7_transcript()
        home = (DESKTOP_M7_HOME_MARKER + "\n").encode()
        search = (DESKTOP_M7_SEARCH_MARKER + "\n").encode()
        cases = (
            valid.replace(home, b""),
            valid.replace(search, search * 2),
            valid.replace(home + search, search + home),
            valid + b"DEBIAN_BROWSER_M7_FAIL reason=search-title-timeout\n",
        )

        for transcript in cases:
            with self.subTest(transcript=transcript[-220:]):
                result = classify_desktop_m7_baidu(
                    transcript, expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)


class DebianDesktopM7BaiduGuestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _environment(
        self, *, mode: str = "normal"
    ) -> tuple[dict[str, str], Path, Path]:
        fake_bin = self.directory / f"bin-{mode}"
        fake_bin.mkdir()
        console = self.directory / f"console-{mode}"
        console.write_text("", encoding="utf-8")
        state = self.directory / f"state-{mode}"
        state.write_text("0", encoding="utf-8")
        browser_state = self.directory / f"browser-state-{mode}"
        browser_state.write_text("running", encoding="utf-8")
        netsurf_log = self.directory / f"netsurf-{mode}.log"
        netsurf_log.write_text("netsurf-log-ready\n", encoding="utf-8")
        actions = self.directory / f"actions-{mode}"
        proc_root = self.directory / f"proc-{mode}"
        process = proc_root / "777"
        process.mkdir(parents=True)
        (process / "cmdline").write_bytes(b"/usr/bin/netsurf-gtk\0")

        (fake_bin / "pgrep").write_text(
            "#!/bin/sh\n"
            '[ "$(cat "$ASTERINAS_M7_BROWSER_STATE")" = closed ] && exit 1\n'
            "printf '777\\n'\n"
        )
        (fake_bin / "runuser").write_text(
            "#!/bin/sh\n"
            'printf \'runuser %s\\n\' "$*" >>"$ASTERINAS_M7_ACTIONS"\n'
            "printf 'launched' >\"$ASTERINAS_M7_BROWSER_STATE\"\n"
            "printf '1' >\"$ASTERINAS_M7_STATE\"\n"
            "printf 'netsurf-log-ready\\n' >\"$ASTERINAS_BROWSER_M7_NETSURF_LOG\"\n"
        )
        (fake_bin / "xdotool").write_text(
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$ASTERINAS_M7_ACTIONS"
case "$1" in
  search) printf '42\n' ;;
  getwindowname)
    phase="$(cat "$ASTERINAS_M7_STATE")"
    if [ "$ASTERINAS_M7_MODE" = home-timeout ] && [ "$phase" = 1 ]; then
      printf 'NetSurf\n'
    elif [ "$ASTERINAS_M7_MODE" = search-timeout ] && [ "$phase" = 2 ]; then
      printf 'Baidu - NetSurf\n'
    elif [ "$phase" = 0 ]; then
      printf 'ASTERINAS_JS_PASS - NetSurf\n'
    elif [ "$phase" = 1 ]; then
      printf '百度一下，你就知道 - NetSurf\n'
    else
      printf 'asterinas - 百度 - NetSurf\n'
    fi
    ;;
  windowactivate|windowfocus|mousemove|click|type) ;;
  key)
    if [ "${2-}" = ctrl+q ]; then
      printf 'closed' >"$ASTERINAS_M7_BROWSER_STATE"
    elif [ "${2-}" = Return ]; then
      phase="$(cat "$ASTERINAS_M7_STATE")"
      printf '%s' "$((phase + 1))" >"$ASTERINAS_M7_STATE"
      if [ "$ASTERINAS_M7_MODE" != search-timeout ]; then
        printf 'browser_window_navigate: url https://m.baidu.com/s?word=asterinas\n' \
          >>"$ASTERINAS_BROWSER_M7_NETSURF_LOG"
      fi
    fi
    ;;
  *) exit 8 ;;
esac
""",
            encoding="utf-8",
        )
        for command in ("pgrep", "runuser", "xdotool"):
            (fake_bin / command).chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_BROWSER_M7_CONSOLE=str(console),
            ASTERINAS_BROWSER_M7_PROC_ROOT=str(proc_root),
            ASTERINAS_BROWSER_M7_TIMEOUT_SECONDS="1",
            ASTERINAS_BROWSER_M7_COMMAND_TIMEOUT_SECONDS="1",
            ASTERINAS_BROWSER_M7_CAPTURE_DELAY_SECONDS="0",
            ASTERINAS_BROWSER_M7_FOCUS_DELAY_SECONDS="0",
            ASTERINAS_BROWSER_M7_POLL_DELAY_SECONDS="0",
            ASTERINAS_BROWSER_M7_NETSURF_LOG=str(netsurf_log),
            ASTERINAS_M7_ACTIONS=str(actions),
            ASTERINAS_M7_BROWSER_STATE=str(browser_state),
            ASTERINAS_M7_STATE=str(state),
            ASTERINAS_M7_MODE=mode,
        )
        return environment, console, actions

    def test_guest_navigates_real_homepage_and_submits_search(self) -> None:
        environment, console, actions = self._environment()

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
                DESKTOP_M7_HOME_MARKER,
                DESKTOP_M7_SEARCH_MARKER,
                DESKTOP_M7_READY_MARKER,
            ],
        )
        action_lines = actions.read_text(encoding="utf-8").splitlines()
        self.assertIn("search --onlyvisible --class NetSurf", action_lines)
        self.assertIn("key ctrl+q", action_lines)
        runuser_action = next(
            action for action in action_lines if action.startswith("runuser ")
        )
        self.assertIn("--enable_javascript=0", runuser_action)
        self.assertTrue(runuser_action.endswith(" https://m.baidu.com/"))
        self.assertLess(
            action_lines.index("windowactivate --sync 42"),
            action_lines.index("windowfocus --sync 42"),
        )
        self.assertLess(
            action_lines.index("windowfocus --sync 42"),
            action_lines.index("mousemove --sync --window 42 500 42"),
        )
        search_focus = action_lines.index("mousemove --sync --window 42 500 42")
        self.assertEqual(
            action_lines[search_focus : search_focus + 5],
            [
                "mousemove --sync --window 42 500 42",
                "click 1",
                "key ctrl+a",
                "type --delay 0 -- https://m.baidu.com/s?word=asterinas",
                "key Return",
            ],
        )
        self.assertNotIn("mousemove --sync 500 42", action_lines)
        self.assertNotIn("mousemove --sync 560 310", action_lines)
        self.assertEqual(action_lines.count("key Return"), 1)

    def test_guest_reports_bounded_home_and_search_navigation_failures(self) -> None:
        for mode, reason in (
            ("home-timeout", "home-title-timeout"),
            ("search-timeout", "search-navigation-timeout"),
        ):
            with self.subTest(mode=mode):
                environment, console, _ = self._environment(mode=mode)
                result = subprocess.run(
                    ["/bin/bash", str(EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                lines = console.read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    lines[-3],
                    "DEBIAN_BROWSER_M7_NETSURF_LOG "
                    "tail_hex=6e6574737572662d6c6f672d72656164790a",
                )
                self.assertTrue(lines[-2].startswith("DEBIAN_BROWSER_M7_DIAGNOSTIC"))
                self.assertEqual(lines[-1], f"DEBIAN_BROWSER_M7_FAIL reason={reason}")

    def test_builder_installs_m7_service_after_m6(self) -> None:
        builder = ROOTFS_BUILDER.read_text(encoding="utf-8")

        self.assertIn("desktop_m7_baidu_evidence.sh", builder)
        self.assertIn("/usr/lib/asterinas/desktop-m7-baidu-evidence", builder)
        self.assertIn("After=asterinas-desktop-m6-browser.service", builder)
        self.assertIn("Environment=ASTERINAS_BROWSER_M7_TIMEOUT_SECONDS=180", builder)
        self.assertIn("TimeoutStartSec=240", builder)
        self.assertIn("Environment=ASTERINAS_DESKTOP_BROWSER_VERBOSE=1", builder)


class DebianDesktopM7BaiduAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _operations(self) -> DesktopM7BaiduOperations:
        operations = object.__new__(DesktopM7BaiduOperations)
        operations._home_screenshot = b""
        operations._home_screenshot_metadata = {}
        operations._search_screenshot = b""
        operations._search_screenshot_metadata = {}
        operations._failure_screenshot = b""
        operations._failure_screenshot_metadata = {}
        return operations

    def test_protocol_captures_home_then_search_after_m6(self) -> None:
        operations = self._operations()
        events: list[object] = []

        self.assertEqual(
            DesktopM7BaiduOperations.FAILURE_MARKER,
            b"DEBIAN_BROWSER_M6_FAIL reason=",
        )

        class Serial:
            def wait_for_any(
                self, markers: tuple[bytes, ...], deadline: float
            ) -> bytes:
                del deadline
                events.append(markers)
                if DESKTOP_M7_HOME_MARKER.encode() in markers:
                    return DESKTOP_M7_HOME_MARKER.encode()
                return DESKTOP_M7_SEARCH_MARKER.encode()

            def wait_for(self, marker: bytes, deadline: float) -> bytes:
                del deadline
                events.append(marker)
                return marker

        session = {
            "serial": Serial(),
            "monitor": object(),
            "directory": self.directory,
        }
        config = SimpleNamespace(boot_timeout=2.0, command_timeout=1.0)
        metadata = {"width": 1280, "height": 1024}
        captures = [
            (b"home-ppm", metadata),
            (b"search-ppm", metadata),
        ]

        with (
            mock.patch.object(
                DesktopM6BrowserOperations,
                "run_protocol",
                autospec=True,
                side_effect=lambda *_: events.append("m6"),
            ),
            mock.patch.object(
                desktop_m7_baidu_gate,
                "capture_rendered_ppm",
                side_effect=captures,
            ) as capture,
        ):
            operations.run_protocol(session, config)

        self.assertEqual(events[0], "m6")
        self.assertEqual(operations._home_screenshot, b"home-ppm")
        self.assertEqual(operations._search_screenshot, b"search-ppm")
        self.assertEqual(
            [call.args[1].name for call in capture.call_args_list],
            ["desktop-m7-baidu-home.ppm", "desktop-m7-baidu-search.ppm"],
        )

    def test_protocol_captures_current_frame_when_m7_guest_fails(self) -> None:
        operations = self._operations()

        class Serial:
            def wait_for_any(
                self, markers: tuple[bytes, ...], deadline: float
            ) -> bytes:
                del markers, deadline
                return b"DEBIAN_BROWSER_M7_FAIL reason=home-title-timeout"

        session = {
            "serial": Serial(),
            "monitor": object(),
            "directory": self.directory,
        }
        config = SimpleNamespace(boot_timeout=2.0, command_timeout=1.0)

        with (
            mock.patch.object(
                DesktopM6BrowserOperations,
                "run_protocol",
                autospec=True,
            ),
            mock.patch.object(
                desktop_m7_baidu_gate,
                "capture_rendered_ppm",
                return_value=(b"failure-ppm", {"width": 1280}),
            ) as capture,
        ):
            with self.assertRaisesRegex(
                desktop_m7_baidu_gate.GateFailure,
                "guest reported Baidu page failure",
            ):
                operations.run_protocol(session, config)

        self.assertEqual(operations._failure_screenshot, b"failure-ppm")
        self.assertEqual(
            operations._failure_screenshot_metadata,
            {"width": 1280},
        )
        self.assertEqual(
            capture.call_args.args[1].name,
            "desktop-m7-baidu-failure.ppm",
        )

    def test_publish_records_both_m7_frames_and_make_target(self) -> None:
        operations = self._operations()
        operations._home_screenshot = b"home"
        operations._search_screenshot = b"search"
        operations._failure_screenshot = b"failure"
        operations._home_screenshot_metadata = {"width": 1280}
        operations._search_screenshot_metadata = {"width": 1280}
        operations._failure_screenshot_metadata = {"width": 1024}
        writes: list[tuple[str, bytes, int]] = []
        result: dict[str, object] = {}

        class Output:
            def atomic_write(self, name: str, payload: bytes, *, mode: int) -> None:
                writes.append((name, payload, mode))

        operations._require_config = lambda config: None
        operations._require_output = lambda: Output()
        with mock.patch.object(
            DesktopM6BrowserOperations, "publish", autospec=True
        ) as inherited:
            operations.publish(object(), object(), b"transcript", result)

        self.assertEqual(
            writes,
            [
                ("desktop-m7-baidu-home.ppm", b"home", 0o600),
                ("desktop-m7-baidu-search.ppm", b"search", 0o600),
                ("desktop-m7-baidu-failure.ppm", b"failure", 0o600),
            ],
        )
        self.assertEqual(result["homepage_screenshot"], {"width": 1280})
        self.assertEqual(result["search_screenshot"], {"width": 1280})
        self.assertEqual(result["failure_screenshot"], {"width": 1024})
        inherited.assert_called_once()

        makefile = MAKEFILE.read_text(encoding="utf-8")
        target = makefile.split(".PHONY: test_riscv_debian_desktop_m7_baidu_gate", 1)[
            1
        ].split(".PHONY:", 1)[0]
        self.assertIn("desktop_m7_baidu_gate", target)
        self.assertIn("DEBIAN_DESKTOP_M7_BAIDU_GATE_OUTPUT", target)
        self.assertIn('--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', target)


if __name__ == "__main__":
    unittest.main()
