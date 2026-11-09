#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Host runner for the subsystem selftests.

Linux keeps one boot path and validates subsystems inside it; this runner is
the host side of that model. It selects the subsystems from the catalog,
computes the single boot target, and (once the guest bundle exists) boots
once and collects `SELFTEST <subsystem> <case> PASS|FAIL` lines.

Today the guest bundle is not built yet, so `rt selftest` prints the plan and
`--dry-run` is the supported mode. `--list` shows the catalog.
"""

from __future__ import annotations

import argparse
import json
import sys

from tools.riscv.selftests import catalog


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

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        _print_plan(plan)

    if not args.dry_run:
        print(
            "rt selftest: the guest selftest bundle is not built yet; "
            "use --dry-run. See docs/porting/2026-11-09-selftests.md.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
