#!/usr/bin/env python3
"""Audit the CPU properties of a QEMU/U-Boot device tree.

The audit is deliberately read-only.  It uses ``fdtget`` instead of booting a
guest so that an early DT validation failure can be diagnosed from the exact
DTB handed to U-Boot.  The JSON output is suitable for attaching to a gate
receipt and contains the SHA-256 of the inspected DTB.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _fdtget(dtb: Path, node: str, prop: str, value_type: str | None = None) -> str | None:
    command = ["fdtget"]
    if value_type is not None:
        command.extend(["-t", value_type])
    command.extend([str(dtb), node, prop])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def audit(dtb: Path) -> dict[str, object]:
    if shutil.which("fdtget") is None:
        raise RuntimeError("fdtget is required; install device-tree-compiler")
    if not dtb.is_file():
        raise FileNotFoundError(dtb)

    listed = subprocess.run(
        ["fdtget", "-l", str(dtb), "/cpus"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "cannot enumerate /cpus")

    cpus: list[dict[str, object]] = []
    for name in listed.stdout.splitlines():
        name = name.strip()
        if not name or name.split("@", 1)[0] != "cpu":
            continue
        node = f"/cpus/{name}"
        if _fdtget(dtb, node, "device_type", "s") != "cpu":
            continue
        cpus.append(
            {
                "node": node,
                "mmu-type": _fdtget(dtb, node, "mmu-type", "s"),
                "riscv,isa": _fdtget(dtb, node, "riscv,isa", "s"),
                "riscv,cbom-block-size": _fdtget(
                    dtb, node, "riscv,cbom-block-size", "u"
                ),
            }
        )

    properties = {
        "mmu-type": {
            "required_by_current_main": True,
            "observed_on_all_cpu_nodes": bool(cpus)
            and all(item["mmu-type"] is not None for item in cpus),
            "missing_consequence": (
                "current validate_cpu_nodes/for_each_hart_id skips the node; "
                "zero remaining application CPUs panics before UART"
            ),
        },
        "riscv,isa": {
            "required_by_current_main": True,
            "observed_on_all_cpu_nodes": bool(cpus)
            and all(item["riscv,isa"] is not None for item in cpus),
            "missing_consequence": (
                "ISA extension discovery cannot parse that CPU; the current "
                "extension path logs an error and cannot establish its set"
            ),
        },
        "riscv,cbom-block-size": {
            "required_by_current_main": True,
            "observed_on_all_cpu_nodes": bool(cpus)
            and all(item["riscv,cbom-block-size"] is not None for item in cpus),
            "missing_consequence": (
                "current DMA cache-maintenance path expects this property and "
                "panics when it is first needed; the KVM patch adds a 64-byte fallback"
            ),
        },
    }
    return {
        "schema": 1,
        "dtb": str(dtb),
        "dtb_sha256": hashlib.sha256(dtb.read_bytes()).hexdigest(),
        "cpu_nodes": cpus,
        "properties": properties,
        "status": "pass"
        if cpus and all(item["observed_on_all_cpu_nodes"] for item in properties.values())
        else "fail",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dtb", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit(args.dtb)
    except (FileNotFoundError, RuntimeError) as error:
        print(f"qemu-dtb-cpu-contract: {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
