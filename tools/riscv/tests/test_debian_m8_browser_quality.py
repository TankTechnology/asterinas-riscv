#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.megrez_network_fixture import BROWSER_DOWNLOAD_SHA256


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
        [[ "$*" == 'mousemove --window 42 80 240 click 1' ]] || exit 11
        printf '3' >"$ASTERINAS_M8_STATE"
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
            ctrl+l|ctrl+a|End|Home|Tab) : ;;
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
            "key Tab",
            "type --delay 0 -- asterinas",
            "key Return",
            "mousemove --window 42 80 240 click 1",
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
