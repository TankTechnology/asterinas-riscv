#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv.debian.rootfs import desktop_m8_browser_quality_gate
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import DesktopM7BaiduOperations
from tools.riscv.debian.rootfs.desktop_m8_browser_quality_gate import (
    DESKTOP_M8_BROWSER_QUALITY_MILESTONES,
    DESKTOP_M8_CAPTURE_PATTERN,
    DESKTOP_M8_FAILURE_MARKER,
    DESKTOP_M8_FIXED_MILESTONES,
    DESKTOP_M8_READY_MARKER,
    QUALITY_CAPTURE_NAMES,
    DesktopM8BrowserQualityOperations,
    classify_desktop_m8_browser_quality,
)
from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.megrez_network_fixture import BROWSER_DOWNLOAD_SHA256
from tools.riscv.tests.test_debian_m7_baidu import _m7_transcript


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh"
)
ROOTFS_BUILDER = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"
FIXTURE_BASE = "http://127.0.0.1:17894"
FIXTURE_MARKER = "DEBIAN_BROWSER_M8_FIXTURE text=cjk-latin image=png form=query"
SCROLL_MARKER = "DEBIAN_BROWSER_M8_SCROLL direction=end-home"
NAVIGATION_MARKER = (
    "DEBIAN_BROWSER_M8_NAVIGATION second=loaded back=loaded forward=loaded"
)
DOWNLOAD_MARKER = (
    f"DEBIAN_BROWSER_M8_DOWNLOAD bytes=262144 sha256={BROWSER_DOWNLOAD_SHA256}"
)
SOAK_MARKER = "DEBIAN_BROWSER_M8_SOAK seconds=120 process=alive"
CAPTURE_PREFIX = "DEBIAN_BROWSER_M8_CAPTURE bytes="
READY_MARKER = "DEBIAN_BROWSER_M8_READY quality=lightweight"


def _m8_transcript(capture: bytes = b"gzip-xwd") -> bytes:
    capture_marker = (
        f"DEBIAN_BROWSER_M8_CAPTURE bytes={len(capture)} "
        f"sha256={hashlib.sha256(capture).hexdigest()}\n"
    ).encode()
    return (
        _m7_transcript()
        + b"\n".join(DESKTOP_M8_FIXED_MILESTONES)
        + b"\n"
        + capture_marker
        + DESKTOP_M8_READY_MARKER.encode()
        + b"\n"
    )


class DebianDesktopM8BrowserQualityGateTests(unittest.TestCase):
    def _operations(self) -> DesktopM8BrowserQualityOperations:
        operations = object.__new__(DesktopM8BrowserQualityOperations)
        operations._quality_screenshots = {
            name: (b"", {}) for name in QUALITY_CAPTURE_NAMES
        }
        operations._quality_failure_screenshot = b""
        operations._quality_failure_screenshot_metadata = {}
        return operations

    def test_classifier_accepts_one_exact_ordered_m8_contract(self) -> None:
        result = classify_desktop_m8_browser_quality(
            _m8_transcript(), expected_debian_release="13.6"
        )

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(
            DESKTOP_M8_BROWSER_QUALITY_MILESTONES,
            (
                FIXTURE_MARKER,
                SCROLL_MARKER,
                NAVIGATION_MARKER,
                DOWNLOAD_MARKER,
                SOAK_MARKER,
                CAPTURE_PREFIX,
                READY_MARKER,
            ),
        )
        capture = DESKTOP_M8_CAPTURE_PATTERN.search(_m8_transcript())
        self.assertIsNotNone(capture)
        assert capture is not None
        self.assertEqual(int(capture.group(1)), len(b"gzip-xwd"))

    def test_classifier_rejects_invalid_m8_evidence(self) -> None:
        valid = _m8_transcript()
        fixture = DESKTOP_M8_FIXED_MILESTONES[0] + b"\n"
        scroll = DESKTOP_M8_FIXED_MILESTONES[1] + b"\n"
        capture = DESKTOP_M8_CAPTURE_PATTERN.search(valid)
        self.assertIsNotNone(capture)
        assert capture is not None
        capture_line = capture.group(0)
        cases = (
            valid.replace(fixture, b"", 1),
            valid.replace(fixture, fixture * 2, 1),
            valid.replace(fixture + scroll, scroll + fixture, 1),
            valid[len(_m7_transcript()) :] + _m7_transcript(),
            valid + DESKTOP_M8_FAILURE_MARKER + b"title-timeout\n",
            valid.replace(capture_line, b"", 1),
            valid.replace(capture_line, capture_line * 2, 1),
            valid + b"DEBIAN_BROWSER_M8_CAPTURE bytes=0 sha256=bad\n",
            valid.replace(
                capture_line, b"DEBIAN_BROWSER_M8_CAPTURE bytes=0 sha256=bad\n"
            ),
        )

        for transcript in cases:
            with self.subTest(transcript=transcript[-240:]):
                result = classify_desktop_m8_browser_quality(
                    transcript, expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)

    def test_protocol_captures_four_ordered_quality_states_after_m7(self) -> None:
        operations = self._operations()
        events: list[object] = []
        marker_queue = [
            *DESKTOP_M8_FIXED_MILESTONES,
            b"DEBIAN_BROWSER_M8_CAPTURE bytes=8 "
            + hashlib.sha256(b"gzip-xwd").hexdigest().encode(),
            DESKTOP_M8_READY_MARKER.encode(),
        ]

        class Serial:
            def checkpoint(self) -> int:
                return 37

            def wait_for_any(
                self,
                markers: tuple[bytes, ...],
                deadline: float,
                *,
                start: int = 0,
            ) -> bytes:
                del deadline
                self_outer.assertEqual(start, 37)
                observed = marker_queue.pop(0)
                self_outer.assertTrue(
                    any(observed.startswith(marker) for marker in markers),
                    (observed, markers),
                )
                events.append(observed)
                return observed

        self_outer = self
        session = {
            "serial": Serial(),
            "monitor": object(),
            "directory": Path("/private/session"),
        }
        config = SimpleNamespace(boot_timeout=2.0, command_timeout=1.0)
        captures = [
            (f"ppm-{index}".encode(), {"width": 1280, "height": 1024})
            for index in range(4)
        ]

        with (
            mock.patch.object(
                DesktopM7BaiduOperations,
                "run_protocol",
                autospec=True,
                side_effect=lambda *_: events.append("m7"),
            ),
            mock.patch.object(
                desktop_m8_browser_quality_gate,
                "capture_rendered_ppm",
                side_effect=captures,
            ) as capture_ppm,
        ):
            operations.run_protocol(session, config)

        self.assertEqual(events[0], "m7")
        self.assertEqual(marker_queue, [])
        self.assertEqual(
            [call.args[1].name for call in capture_ppm.call_args_list],
            list(QUALITY_CAPTURE_NAMES),
        )
        self.assertEqual(
            [
                operations._quality_screenshots[name][0]
                for name in QUALITY_CAPTURE_NAMES
            ],
            [item[0] for item in captures],
        )

    def test_protocol_captures_failure_frame(self) -> None:
        operations = self._operations()
        operations._failure_screenshot = b"m7-failure"
        operations._failure_screenshot_metadata = {"width": 800}

        class Serial:
            def checkpoint(self) -> int:
                return 0

            def wait_for_any(
                self,
                markers: tuple[bytes, ...],
                deadline: float,
                *,
                start: int = 0,
            ) -> bytes:
                del markers, deadline, start
                return DESKTOP_M8_FAILURE_MARKER + b"title-timeout"

        session = {
            "serial": Serial(),
            "monitor": object(),
            "directory": self.directory if hasattr(self, "directory") else Path("/tmp"),
        }
        config = SimpleNamespace(boot_timeout=2.0, command_timeout=1.0)

        with (
            mock.patch.object(DesktopM7BaiduOperations, "run_protocol", autospec=True),
            mock.patch.object(
                desktop_m8_browser_quality_gate,
                "capture_rendered_ppm",
                return_value=(b"failure-ppm", {"width": 1024}),
            ) as capture_ppm,
        ):
            with self.assertRaisesRegex(
                desktop_m8_browser_quality_gate.GateFailure,
                "guest reported browser quality failure",
            ):
                operations.run_protocol(session, config)

        self.assertEqual(operations._failure_screenshot, b"m7-failure")
        self.assertEqual(operations._failure_screenshot_metadata, {"width": 800})
        self.assertEqual(operations._quality_failure_screenshot, b"failure-ppm")
        self.assertEqual(
            operations._quality_failure_screenshot_metadata,
            {"width": 1024},
        )
        self.assertEqual(
            capture_ppm.call_args.args[1].name,
            "desktop-m8-failure.ppm",
        )

    def test_publish_binds_uploaded_capture_and_writes_result_last(self) -> None:
        payload = b"gzip-xwd"
        operations = self._operations()
        operations._quality_screenshots = {
            name: (name.encode(), {"width": 1280, "height": 1024})
            for name in QUALITY_CAPTURE_NAMES
        }
        operations.fixture = SimpleNamespace(
            capture_summary=lambda: {
                "bytes": len(payload),
                "path": "/browser-quality/capture.xwd.gz",
                "peer": "127.0.0.1",
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            capture_payload=lambda: payload,
        )
        writes: list[tuple[str, bytes, int]] = []
        result: dict[str, object] = {"passed": True, "reason": "pass"}

        class Output:
            def atomic_write(
                self, name: str, data: bytes, *, mode: int = 0o600
            ) -> None:
                writes.append((name, data, mode))

        operations._require_config = lambda config: None
        operations._require_output = lambda: Output()
        with mock.patch.object(
            DesktopM7BaiduOperations,
            "publish",
            autospec=True,
            side_effect=lambda *_: writes.append(("result.json", b"result", 0o600)),
        ) as inherited:
            operations.publish(object(), object(), _m8_transcript(payload), result)

        self.assertTrue(result["passed"])
        self.assertEqual(
            [name for name, _, _ in writes[:-1]], list(QUALITY_CAPTURE_NAMES)
        )
        self.assertEqual(writes[-1][0], "result.json")
        for name in QUALITY_CAPTURE_NAMES:
            self.assertEqual(
                result["screenshots"][name], {"width": 1280, "height": 1024}
            )
        inherited.assert_called_once()

        for summary, captured, reason in (
            (None, None, "browser capture missing"),
            (
                {
                    "bytes": len(payload),
                    "sha256": "0" * 64,
                },
                payload,
                "browser capture evidence mismatch",
            ),
            (
                {
                    "bytes": len(payload),
                    "path": "/wrong",
                    "peer": "127.0.0.1",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
                payload,
                "browser capture evidence mismatch",
            ),
            (
                {
                    "bytes": len(payload),
                    "path": "/browser-quality/capture.xwd.gz",
                    "peer": "127.0.0.2",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
                payload,
                "browser capture evidence mismatch",
            ),
        ):
            with self.subTest(reason=reason):
                failed = self._operations()
                failed.fixture = SimpleNamespace(
                    capture_summary=lambda value=summary: value,
                    capture_payload=lambda value=captured: value,
                )
                failed._require_config = lambda config: None
                failed._require_output = lambda: Output()
                failure_result: dict[str, object] = {
                    "passed": True,
                    "reason": "pass",
                }
                with mock.patch.object(DesktopM7BaiduOperations, "publish"):
                    failed.publish(
                        object(), object(), _m8_transcript(payload), failure_result
                    )
                self.assertEqual(failure_result["passed"], False)
                self.assertEqual(failure_result["reason"], reason)

    def test_invalidation_and_make_target_cover_every_m8_artifact(self) -> None:
        operations = self._operations()
        invalidated: list[str] = []
        operations._require_output = lambda: SimpleNamespace(
            invalidate=lambda *names: invalidated.extend(names)
        )
        with mock.patch.object(DesktopM7BaiduOperations, "invalidate"):
            operations.invalidate(object())

        self.assertEqual(
            invalidated,
            [*QUALITY_CAPTURE_NAMES, "desktop-m8-failure.ppm"],
        )
        makefile = MAKEFILE.read_text(encoding="utf-8")
        target = makefile.split(
            ".PHONY: test_riscv_debian_desktop_m8_browser_quality_gate", 1
        )[1].split(".PHONY:", 1)[0]
        self.assertIn("desktop_m8_browser_quality_gate", target)
        self.assertIn("DEBIAN_DESKTOP_M8_BROWSER_QUALITY_GATE_OUTPUT", target)
        self.assertIn('--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', target)


class DebianDesktopM8BrowserQualityGuestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _write_tool(self, directory: Path, name: str, contents: str) -> None:
        path = directory / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _environment(
        self, mode: str = "normal"
    ) -> tuple[dict[str, str], dict[str, Path]]:
        fake_bin = self.directory / f"bin-{mode}"
        fake_bin.mkdir()
        paths = {
            name: self.directory / f"{name}-{mode}"
            for name in (
                "actions",
                "browser_state",
                "capture",
                "console",
                "curl_log",
                "download",
                "download_source",
                "gzip_log",
                "soak_done",
                "state",
                "typed",
                "upload",
                "xwd_log",
            )
        }
        paths["console"].write_text("", encoding="utf-8")
        paths["browser_state"].write_text("running", encoding="utf-8")
        paths["state"].write_text("0", encoding="utf-8")
        paths["download_source"].write_bytes(bytes(range(256)) * 1024)
        proc_root = self.directory / f"proc-{mode}"
        process = proc_root / "777"
        process.mkdir(parents=True)
        (process / "cmdline").write_bytes(b"/usr/bin/netsurf-gtk\0")

        self._write_tool(
            fake_bin,
            "pgrep",
            """#!/bin/bash
set -eu
if [[ "$ASTERINAS_M8_MODE" == browser-exit && -e "$ASTERINAS_M8_SOAK_DONE" ]]; then
    exit 1
fi
printf '777\n'
""",
        )
        self._write_tool(
            fake_bin,
            "xprop",
            """#!/bin/bash
set -eu
[[ "$*" == '-id 42 _NET_WM_PID' ]] || exit 9
printf '_NET_WM_PID(CARDINAL) = 777\n'
""",
        )
        self._write_tool(
            fake_bin,
            "xdotool",
            """#!/bin/bash
set -eu
printf '%s\n' "$*" >>"$ASTERINAS_M8_ACTIONS"
case "$1" in
    search)
        if [[ "$*" == 'search --onlyvisible --classname ^netsurf-gtk$' ]]; then
            if [[ "$ASTERINAS_M8_MODE" == ambiguous-window ]]; then
                printf '42\n43\n'
            else
                printf '42\n'
            fi
        elif [[ "$*" == 'search --onlyvisible --name ^Save File$' ]]; then
            printf '84\n'
        else
            exit 10
        fi
        ;;
    getwindowname)
        phase="$(cat "$ASTERINAS_M8_STATE")"
        if [[ "$ASTERINAS_M8_MODE" == title-timeout && "$phase" == 1 ]]; then
            printf 'Loading - NetSurf\n'
        elif [[ "$phase" == 1 ]]; then
            printf 'Asterinas Browser Quality - NetSurf\n'
        elif [[ "$phase" == 2 ]]; then
            printf 'asterinas - Asterinas Browser Quality - NetSurf\n'
        elif [[ "$phase" == 3 ]]; then
            printf 'Second - Asterinas Browser Quality - NetSurf\n'
        else
            printf 'Baidu - NetSurf\n'
        fi
        ;;
    type)
        value="${@: -1}"
        printf '%s' "$value" >"$ASTERINAS_M8_TYPED"
        ;;
    mousemove)
        case "$*" in
            'mousemove --window 42 100 165 click 1') : ;;
            'mousemove --window 42 90 313 click 1')
                printf '3' >"$ASTERINAS_M8_STATE"
                ;;
            *) exit 11 ;;
        esac
        ;;
    key)
        case "${2-}" in
            Return)
                value="$(cat "$ASTERINAS_M8_TYPED")"
                case "$value" in
                    */browser-quality/index.html) printf '1' >"$ASTERINAS_M8_STATE" ;;
                    asterinas) printf '2' >"$ASTERINAS_M8_STATE" ;;
                    */browser-quality/download.bin) : ;;
                    "$ASTERINAS_M8_DOWNLOAD")
                        if [[ "$ASTERINAS_M8_MODE" == download-mismatch ]]; then
                            printf 'wrong-download' >"$ASTERINAS_M8_DOWNLOAD"
                        else
                            cp -- "$ASTERINAS_M8_DOWNLOAD_SOURCE" "$ASTERINAS_M8_DOWNLOAD"
                        fi
                        ;;
                    *) exit 12 ;;
                esac
                ;;
            alt+Left) printf '2' >"$ASTERINAS_M8_STATE" ;;
            alt+Right) printf '3' >"$ASTERINAS_M8_STATE" ;;
            ctrl+l|ctrl+a|End|Home) : ;;
            *) exit 13 ;;
        esac
        ;;
    windowactivate|windowfocus) : ;;
    *) exit 14 ;;
esac
""",
        )
        self._write_tool(
            fake_bin,
            "sleep",
            """#!/bin/bash
set -eu
if [[ "$1" == 120 ]]; then
    : >"$ASTERINAS_M8_SOAK_DONE"
    exit 0
fi
exec /usr/bin/sleep "$@"
""",
        )
        self._write_tool(
            fake_bin,
            "xwd",
            """#!/bin/bash
set -eu
printf '%s\n' "$*" >>"$ASTERINAS_M8_XWD_LOG"
[[ "$ASTERINAS_M8_MODE" != xwd-failure ]] || exit 17
printf 'deterministic-xwd'
""",
        )
        self._write_tool(
            fake_bin,
            "gzip",
            """#!/bin/bash
set -eu
printf '%s\n' "$*" >>"$ASTERINAS_M8_GZIP_LOG"
exec /usr/bin/gzip "$@"
""",
        )
        self._write_tool(
            fake_bin,
            "curl",
            """#!/bin/bash
set -eu
printf '%s\n' "$*" >>"$ASTERINAS_M8_CURL_LOG"
[[ "$ASTERINAS_M8_MODE" != upload-rejected ]] || exit 22
while (($#)); do
    if [[ "$1" == --data-binary ]]; then
        shift
        cp -- "${1#@}" "$ASTERINAS_M8_UPLOAD"
    fi
    shift
done
""",
        )

        if mode == "stale-download":
            paths["download"].write_text("stale", encoding="utf-8")

        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_BROWSER_M8_CONSOLE=str(paths["console"]),
            ASTERINAS_BROWSER_M8_FIXTURE_URL=FIXTURE_BASE,
            ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS=(
                "1" if mode == "title-timeout" else "10"
            ),
            ASTERINAS_BROWSER_M8_COMMAND_TIMEOUT_SECONDS="1",
            ASTERINAS_BROWSER_M8_POLL_DELAY_SECONDS="0",
            ASTERINAS_BROWSER_M8_SETTLE_DELAY_SECONDS="0",
            ASTERINAS_BROWSER_M8_DOWNLOAD=str(paths["download"]),
            ASTERINAS_BROWSER_M8_CAPTURE=str(paths["capture"]),
            ASTERINAS_BROWSER_M8_PROC_ROOT=str(proc_root),
            ASTERINAS_M8_ACTIONS=str(paths["actions"]),
            ASTERINAS_M8_BROWSER_STATE=str(paths["browser_state"]),
            ASTERINAS_M8_CURL_LOG=str(paths["curl_log"]),
            ASTERINAS_M8_DOWNLOAD=str(paths["download"]),
            ASTERINAS_M8_DOWNLOAD_SOURCE=str(paths["download_source"]),
            ASTERINAS_M8_GZIP_LOG=str(paths["gzip_log"]),
            ASTERINAS_M8_MODE=mode,
            ASTERINAS_M8_SOAK_DONE=str(paths["soak_done"]),
            ASTERINAS_M8_STATE=str(paths["state"]),
            ASTERINAS_M8_TYPED=str(paths["typed"]),
            ASTERINAS_M8_UPLOAD=str(paths["upload"]),
            ASTERINAS_M8_XWD_LOG=str(paths["xwd_log"]),
        )
        return environment, paths

    def _run(
        self, mode: str = "normal"
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
        environment, paths = self._environment(mode)
        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result, paths

    def test_guest_drives_bounded_browser_quality_contract(self) -> None:
        result, paths = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = paths["console"].read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines[:5],
            [
                FIXTURE_MARKER,
                SCROLL_MARKER,
                NAVIGATION_MARKER,
                DOWNLOAD_MARKER,
                SOAK_MARKER,
            ],
        )
        self.assertTrue(lines[5].startswith(CAPTURE_PREFIX))
        self.assertEqual(lines[6], READY_MARKER)
        capture_parts = dict(part.split("=", 1) for part in lines[5].split()[1:])
        uploaded = paths["upload"].read_bytes()
        self.assertEqual(int(capture_parts["bytes"]), len(uploaded))
        self.assertEqual(capture_parts["sha256"], hashlib.sha256(uploaded).hexdigest())
        actions = paths["actions"].read_text(encoding="utf-8").splitlines()
        expected = [
            "key ctrl+l",
            f"type --delay 0 -- {FIXTURE_BASE}/browser-quality/index.html",
            "key Return",
            "key End",
            "key Home",
            "mousemove --window 42 100 165 click 1",
            "type --delay 0 -- asterinas",
            "key Return",
            "mousemove --window 42 90 313 click 1",
            "key alt+Left",
            "key alt+Right",
            "key ctrl+l",
            f"type --delay 0 -- {FIXTURE_BASE}/browser-quality/download.bin",
            "key Return",
            "search --onlyvisible --name ^Save File$",
            "windowactivate --sync 84",
            "key ctrl+a",
            f"type --delay 0 -- {paths['download']}",
            "key Return",
        ]
        positions: list[int] = []
        cursor = 0
        for action in expected:
            position = actions.index(action, cursor)
            positions.append(position)
            cursor = position + 1
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            paths["xwd_log"].read_text().splitlines(), ["-display :0 -root -silent"]
        )
        self.assertEqual(paths["gzip_log"].read_text().splitlines(), ["-n"])
        self.assertEqual(len(paths["curl_log"].read_text().splitlines()), 1)
        self.assertEqual(
            actions.count("search --onlyvisible --classname ^netsurf-gtk$"), 7
        )

    def test_guest_reports_one_stable_failure_without_ready(self) -> None:
        cases = (
            ("ambiguous-window", "ambiguous-window"),
            ("title-timeout", "title-timeout"),
            ("stale-download", "stale-download"),
            ("download-mismatch", "download-mismatch"),
            ("browser-exit", "browser-exit"),
            ("xwd-failure", "xwd-failure"),
            ("upload-rejected", "upload-rejected"),
        )
        for mode, reason in cases:
            with self.subTest(mode=mode):
                result, paths = self._run(mode)
                self.assertNotEqual(result.returncode, 0)
                lines = paths["console"].read_text(encoding="utf-8").splitlines()
                failures = [
                    line for line in lines if line.startswith("DEBIAN_BROWSER_M8_FAIL")
                ]
                self.assertEqual(failures, [f"DEBIAN_BROWSER_M8_FAIL reason={reason}"])
                self.assertNotIn(READY_MARKER, lines)

    def test_guest_rejects_invalid_environment_before_browser_actions(self) -> None:
        environment, paths = self._environment()
        environment["ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS"] = "0"

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            paths["console"].read_text(encoding="utf-8").splitlines(),
            ["DEBIAN_BROWSER_M8_FAIL reason=invalid-timeout"],
        )
        self.assertFalse(paths["actions"].exists())

    def test_profile_builder_and_unit_target_install_m8_contract(self) -> None:
        profile = get_profile("desktop-m5-network")
        self.assertIn("x11-apps", profile.requested_packages)
        self.assertIn("x11-apps", profile.identity_packages)

        builder = ROOTFS_BUILDER.read_text(encoding="utf-8")
        self.assertIn("desktop_m8_browser_quality_evidence.sh", builder)
        self.assertIn("/usr/lib/asterinas/desktop-m8-browser-quality-evidence", builder)
        self.assertIn("After=asterinas-desktop-m7-baidu.service", builder)
        self.assertIn("Environment=ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS=300", builder)
        self.assertIn("TimeoutStartSec=360", builder)
        self.assertIn(
            "ExecStart=/usr/lib/asterinas/desktop-m8-browser-quality-evidence",
            builder,
        )
        self.assertIn(
            "tools.riscv.tests.test_debian_m8_browser_quality",
            MAKEFILE.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
