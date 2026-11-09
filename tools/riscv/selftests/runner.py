#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Host runner for the subsystem selftests.

Linux keeps one boot path and validates subsystems inside it; this runner is
the host side of that model. It selects the subsystems from the catalog,
computes the single boot target, pushes the guest bundle over the serial
bridge, and collects `SELFTEST <subsystem> <case> PASS|FAIL|SKIP` lines.

    rt selftest --list
    rt selftest console block net --dry-run
    rt selftest --board console block net --http-url http://host:18080/

Without `--board` the runner only prints the plan; the QEMU transport is the
next stage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from tools.riscv.selftests import catalog

GUEST_RUNNER = Path(__file__).with_name("guest") / "run.sh"
REMOTE_RUNNER = "/tmp/asterinas-selftest.sh"
RESULT_LINE = re.compile(
    r"^SELFTEST (\S+) (\S+) (PASS|FAIL|SKIP)(?: (.*))?$", re.MULTILINE
)
DONE_LINE = re.compile(r"^SELFTEST_DONE$", re.MULTILINE)


def build_plan(names: list[str], profile: str) -> dict[str, object]:
    subsystems = catalog.select(names)
    target = catalog.boot_target(subsystems)
    return {
        "profile": profile,
        "target": target,
        "subsystems": [
            {
                "name": subsystem.name,
                "target": subsystem.target,
                "cases": [case.name for case in subsystem.cases],
            }
            for subsystem in subsystems
        ],
    }


def parse_results(text: str) -> list[dict[str, str]]:
    return [
        {
            "subsystem": match.group(1),
            "case": match.group(2),
            "status": match.group(3),
            "detail": (match.group(4) or "").strip(),
        }
        for match in RESULT_LINE.finditer(text)
    ]


def missing_cases(
    plan: dict[str, object], results: list[dict[str, str]]
) -> list[tuple[str, str]]:
    """Return planned (subsystem, case) pairs with no result line.

    A catalog entry can exist before its guest bundle does; that must fail the
    run instead of passing silently with zero checks.
    """

    seen = {(result["subsystem"], result["case"]) for result in results}
    expected = [
        (str(item["name"]), str(case))
        for item in plan["subsystems"]
        for case in item["cases"]
    ]
    return [pair for pair in expected if pair not in seen]


def _print_catalog() -> None:
    for subsystem in catalog.SUBSYSTEMS:
        print(f"{subsystem.name:10s} target={subsystem.target}")
        for case in subsystem.cases:
            print(f"  {case.name:14s} {case.summary}")


def _print_plan(plan: dict[str, object]) -> None:
    print(f"profile: {plan['profile']}")
    print(f"boot target: {plan['target']}")
    ordered = ", ".join(str(item["name"]) for item in plan["subsystems"])
    print(f"subsystems (in order): {ordered}")
    for item in plan["subsystems"]:
        print(f"  {item['name']}: {', '.join(item['cases'])}")


def _wait_quiet(console, log_offset: int, gap: float = 4.0, timeout: float = 120.0) -> int:
    end = time.time() + timeout
    last = time.time()
    offset = log_offset
    while time.time() < end:
        chunk, offset = console.read_since(offset)
        if chunk:
            last = time.time()
        elif time.time() - last >= gap:
            return offset
        time.sleep(0.5)
    return offset


def run_board(args: argparse.Namespace, plan: dict[str, object]) -> int:
    from tools.riscv.board.megrez_console import Console

    console = Console(args.log, args.fifo)
    offset = _wait_quiet(console, os.path.getsize(args.log))

    if not console.push_verified(str(GUEST_RUNNER), REMOTE_RUNNER):
        print(
            "rt selftest: guest bundle push failed or arrived truncated",
            file=sys.stderr,
        )
        return 2

    subsystems = " ".join(str(item["name"]) for item in plan["subsystems"])
    env = [f"SELFTEST_EXPECT_EPOCH={int(time.time())}"]
    if args.http_url:
        env.append(f"SELFTEST_HTTP_URL={args.http_url}")
    if args.dns_host:
        env.append(f"SELFTEST_DNS_HOST={args.dns_host}")
    command = " ".join([*env, "sh", REMOTE_RUNNER, subsystems]).strip()
    console.type_line(command)

    text = ""
    end = time.time() + args.timeout
    while time.time() < end:
        chunk, offset = console.read_since(offset)
        text += chunk
        if DONE_LINE.search(text):
            break
        time.sleep(0.5)

    results = parse_results(text)
    # A case with no result line is a failure, not silence: during the
    # migration some subsystems have a catalog entry but no guest bundle yet.
    missing = missing_cases(plan, results)

    for result in results:
        detail = f" {result['detail']}" if result["detail"] else ""
        print(
            f"{result['status']:4} {result['subsystem']:8s} "
            f"{result['case']:14s}{detail}"
        )
    for subsystem, case in missing:
        print(f"FAIL {subsystem:8s} {case:14s} no result from the guest")
    passed = sum(1 for result in results if result["status"] == "PASS")
    skipped = sum(1 for result in results if result["status"] == "SKIP")
    failed = (
        sum(1 for result in results if result["status"] == "FAIL") + len(missing)
    )
    complete = bool(DONE_LINE.search(text)) and not missing
    print(
        f"SELFTEST total={len(results) + len(missing)} pass={passed} fail={failed} "
        f"skip={skipped} complete={complete}"
    )
    return 0 if complete and failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rt selftest",
        description="Plan or run the subsystem selftests against one booted guest.",
    )
    parser.add_argument(
        "subsystems",
        nargs="*",
        help="subsystem names (default: all, in catalog order)",
    )
    parser.add_argument("--list", action="store_true", help="print the catalog")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the boot target and the ordered checks without booting",
    )
    parser.add_argument("--profile", default="browser-web")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--board",
        action="store_true",
        help="run over the Megrez serial bridge instead of only planning",
    )
    parser.add_argument("--log", default="/tmp/board3.log")
    parser.add_argument("--fifo", default="/tmp/board3.cmd")
    parser.add_argument("--http-url", default="")
    parser.add_argument("--dns-host", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    if args.list:
        if args.json:
            print(
                json.dumps(
                    {
                        "subsystems": [
                            {
                                "name": subsystem.name,
                                "target": subsystem.target,
                                "cases": [case.name for case in subsystem.cases],
                            }
                            for subsystem in catalog.SUBSYSTEMS
                        ]
                    },
                    indent=2,
                )
            )
        else:
            _print_catalog()
        return 0

    names = args.subsystems or [subsystem.name for subsystem in catalog.SUBSYSTEMS]
    try:
        plan = build_plan(names, args.profile)
    except ValueError as error:
        parser.error(str(error))

    if args.json and not args.board:
        print(json.dumps(plan, indent=2))
    elif not args.board:
        _print_plan(plan)

    if args.board:
        return run_board(args, plan)

    if not args.dry_run:
        print(
            "rt selftest: add --board to run over the serial bridge, or "
            "--dry-run to only print the plan.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
