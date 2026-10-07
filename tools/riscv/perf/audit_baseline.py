#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Audit the retained RISC-V Debian performance matrix.

This intentionally fails while a result is only a stacked PR control or while
the LMBench Debian baseline is still missing.  It prevents a partial matrix
from being presented as the completed main-branch baseline.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REQUIRED_RESULTS = {
    "fio_ext4": "stacked_ext4_comparison",
    "sqlite_ext4": "stacked_ext4_sqlite_comparison",
    "iperf3": "stacked_network_comparison",
    "common_ops": "stacked_common_ops_comparison",
    "hackbench": "stacked_hackbench_comparison",
    "schbench": "stacked_schbench_comparison",
}


def audit(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    root = manifest_path.parent
    status = str(manifest.get("comparison_status", ""))
    checks: dict[str, dict] = {}

    for name, key in REQUIRED_RESULTS.items():
        value = manifest.get(key)
        path = root / value if isinstance(value, str) else None
        checks[name] = {
            "present": bool(path and path.is_file()),
            "path": value,
            "stacked_only": "stacked" in status and name != "lmbench",
        }

    # LMBench must be a Debian userspace result, not the earlier Nix/native
    # diagnostic (which is deliberately retained but is not a baseline).
    lmbench_candidates = sorted(root.glob("lmbench-debian-*-comparison*.json"))
    checks["lmbench"] = {
        "present": bool(lmbench_candidates),
        "paths": [p.name for p in lmbench_candidates],
        "diagnostic_only": not bool(lmbench_candidates),
    }

    required_config = set()
    has_qemu = bool(manifest.get("qemu") or manifest.get("qemu_command_args"))
    has_samples = bool(manifest.get("samples") or manifest.get("repeats"))
    has_warmups = "warmups" in manifest
    if not has_qemu:
        required_config.add("qemu or qemu_command_args")
    if not has_samples:
        required_config.add("samples or repeats")
    if not has_warmups:
        required_config.add("warmups")
    checks["provenance"] = {
        "manifest_fields": sorted(required_config - manifest.keys()),
        "has_release_marker": bool(manifest.get("release") is True
                                    or manifest.get("asterinas_release") is True
                                    or "RELEASE=1" in json.dumps(manifest)),
    }

    checks["main_ext4"] = {
        "present": "main_ext4_baseline_complete" in status,
        "reason": "all ext4 comparisons currently identify stacked PR controls"
        if "main_ext4_baseline_complete" not in status else "",
    }
    complete = (checks["lmbench"]["present"]
                and checks["main_ext4"]["present"]
                and all(item["present"] for item in checks.values()
                        if isinstance(item, dict) and "present" in item
                        and item is not checks["main_ext4"])
                and not checks["provenance"]["manifest_fields"]
                and checks["provenance"]["has_release_marker"])
    return {"complete": complete, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.manifest.resolve())
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        print(f"baseline audit failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
