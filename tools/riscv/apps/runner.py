#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Run the Debian application matrix against a booted guest.

    rt apps --list
    rt apps --board
    rt apps --board --install jq mousepad
    rt apps --board --dry-run

The host pushes the probe script and a tab-separated manifest over the serial
bridge, the guest runs each entry, and the host collects `APP <name> PASS|FAIL`
lines. `--install` installs the selected packages first with apt.

Applications are declared in `manifest.py`; the probe is `guest/probe.sh`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from tools.riscv.apps import manifest

PROBE = Path(__file__).with_name("guest") / "probe.sh"
REMOTE_PROBE = "/tmp/asterinas-apps-probe.sh"
REMOTE_MANIFEST = "/tmp/asterinas-apps.tsv"
# The guest reads the manifest with `read` and IFS. TAB cannot be used: it is
# IFS whitespace, so consecutive tabs collapse and an empty field disappears.
# 0x1f (unit separator) is not whitespace and cannot appear in a command.
FIELD_SEPARATOR = "\x1f"
RESULT_LINE = re.compile(r"^APP (\S+) (PASS|FAIL|SKIP)(?: (.*))?$", re.MULTILINE)
DONE_LINE = re.compile(r"^APPS_DONE$", re.MULTILINE)
INSTALL_RC = re.compile(r"APPS_INSTALL_RC=(\d+)")


def render_tsv(apps: list[manifest.App]) -> str:
    rows = [
        FIELD_SEPARATOR.join(
            [app.name, app.kind, app.package, app.command, app.expect, app.window]
        )
        for app in apps
    ]
    return "\n".join(rows) + "\n" if rows else ""


def parse_results(text: str) -> list[dict[str, str]]:
    return [
        {
            "name": match.group(1),
            "status": match.group(2),
            "detail": (match.group(3) or "").strip(),
        }
        for match in RESULT_LINE.finditer(text)
    ]


def build_plan(names: list[str]) -> list[manifest.App]:
    return manifest.select(names) if names else list(manifest.APPS)


def _print_catalog() -> None:
    for app in manifest.APPS:
        print(f"{app.name:12s} {app.kind:3s} package={app.package}")
        print(f"  {app.command}")
        if app.expect:
            print(f"  expect: {app.expect}")
        if app.window:
            print(f"  window: {app.window}")


def _wait_quiet(console, gap: float = 4.0, timeout: float = 120.0) -> None:
    offset = os.path.getsize(console.log)
    end = time.time() + timeout
    last = time.time()
    while time.time() < end:
        chunk, offset = console.read_since(offset)
        if chunk:
            last = time.time()
        elif time.time() - last >= gap:
            return
        time.sleep(0.5)


def _install(console, apps: list[manifest.App], timeout: float) -> int | None:
    packages = manifest.packages(apps)
    if not packages:
        return 0
    command = (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        f"--no-install-recommends {' '.join(packages)}; "
        "rc=$?; apt-get clean; echo APPS_INSTALL_RC=$rc"
    )
    text, complete = console.run(command, timeout=timeout)
    if not complete:
        print("rt apps: install command did not finish", file=sys.stderr)
        return None
    match = INSTALL_RC.search(text)
    if not match:
        print("rt apps: install result marker missing", file=sys.stderr)
        return None
    print(text.strip()[-1200:], file=sys.stderr)
    return int(match.group(1))


def run_board(args: argparse.Namespace, apps: list[manifest.App]) -> int:
    from tools.riscv.board.megrez_console import Console

    console = Console(args.log, args.fifo)
    _wait_quiet(console)

    if args.install:
        rc = _install(console, apps, args.install_timeout)
        if rc != 0:
            print(f"rt apps: package installation failed (rc={rc})", file=sys.stderr)
            return 2

    if not console.push_verified(str(PROBE), REMOTE_PROBE):
        print("rt apps: probe push failed or arrived truncated", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="asterinas-apps-") as directory:
        manifest_path = Path(directory) / "apps.tsv"
        manifest_path.write_text(render_tsv(apps), encoding="utf-8")
        if not console.push_verified(str(manifest_path), REMOTE_MANIFEST):
            print("rt apps: manifest push failed or arrived truncated", file=sys.stderr)
            return 2

    start = os.path.getsize(console.log)
    console.type_line(f"sh {REMOTE_PROBE} {REMOTE_MANIFEST}")
    text = ""
    offset = start
    end = time.time() + args.timeout
    while time.time() < end:
        chunk, offset = console.read_since(offset)
        text += chunk
        if DONE_LINE.search(text):
            break
        time.sleep(0.5)

    results = parse_results(text)
    seen = {result["name"] for result in results}
    missing = [app.name for app in apps if app.name not in seen]

    for result in results:
        detail = f" {result['detail']}" if result["detail"] else ""
        print(f"{result['status']:4} {result['name']:12s}{detail}")
    for name in missing:
        print(f"FAIL {name:12s} no result from the guest")

    passed = sum(1 for result in results if result["status"] == "PASS")
    skipped = sum(1 for result in results if result["status"] == "SKIP")
    failed = (
        sum(1 for result in results if result["status"] == "FAIL") + len(missing)
    )
    complete = bool(DONE_LINE.search(text)) and not missing
    print(
        f"APPS total={len(results) + len(missing)} pass={passed} fail={failed} "
        f"skip={skipped} complete={complete}"
    )
    return 0 if complete and failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rt apps",
        description="Run the Debian application compatibility matrix.",
    )
    parser.add_argument(
        "apps", nargs="*", help="application names (default: all, in manifest order)"
    )
    parser.add_argument("--list", action="store_true", help="print the matrix")
    parser.add_argument(
        "--board",
        action="store_true",
        help="run over the Megrez serial bridge instead of only planning",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="apt-get install the selected packages before probing",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", default="/tmp/board3.log")
    parser.add_argument("--fifo", default="/tmp/board3.cmd")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--install-timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    if args.list:
        if args.json:
            print(
                json.dumps(
                    {
                        "apps": [
                            {
                                "name": app.name,
                                "kind": app.kind,
                                "package": app.package,
                                "command": app.command,
                                "expect": app.expect,
                                "window": app.window,
                            }
                            for app in manifest.APPS
                        ]
                    },
                    indent=2,
                )
            )
        else:
            _print_catalog()
        return 0

    try:
        apps = build_plan(args.apps)
    except ValueError as error:
        parser.error(str(error))

    if args.board:
        return run_board(args, apps)

    if args.json:
        print(
            json.dumps(
                {
                    "apps": [
                        {
                            "name": app.name,
                            "kind": app.kind,
                            "package": app.package,
                        }
                        for app in apps
                    ]
                },
                indent=2,
            )
        )
    else:
        for app in apps:
            print(f"{app.name:12s} {app.kind:3s} package={app.package}")
    if not args.dry_run:
        print(
            "rt apps: add --board to run the matrix, or --dry-run to only plan.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
