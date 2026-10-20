#!/usr/bin/env python3
"""Audit the QEMU evidence for the Asterinas recovery/display/device goal.

This is a read-only evidence joiner. It never starts QEMU, opens a serial
device, modifies a guest image, or treats a host-only fault shim as guest
watchdog evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


_OK_LINE = re.compile(r"^ok\s+\d+\s+", re.MULTILINE)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_first_json(paths: list[Path]) -> tuple[dict[str, Any] | None, Path]:
    """Read the first accessible JSON artifact, preserving its evidence path."""

    for path in paths:
        value = _read_json(path)
        if value is not None:
            return value, path
    return None, paths[0]


def _check(name: str, status: str, reason: str, evidence: str) -> dict[str, str]:
    return {"name": name, "status": status, "reason": reason, "evidence": evidence}


def build_goal_audit(root: Path = Path(".")) -> dict[str, Any]:
    """Join current receipts without upgrading missing or weak evidence."""

    recovery_path = root / "target/qemu-uboot/final-recovery-20261011/receipt.json"
    recovery = _read_json(recovery_path)
    timer_path = root / "target/qemu-guest-faults/timer-disabled-20261011/receipt.json"
    timer = _read_json(timer_path)
    sbi_path = root / "target/qemu-guest-faults/sbi-reset-failure-20261011/receipt.json"
    sbi = _read_json(sbi_path)
    fault_matrix_path = root / "target/qemu-recovery-faults/matrix-20261011/receipt.json"
    fault_matrix = _read_json(fault_matrix_path)
    fb_path = root / "target/qemu-desktop-m4-fb-recovery-20261011-r4/result.json"
    fb_fallback_path = root / "target/qemu-goal-audit-20261011/fb-result.json"
    fb, fb_evidence_path = _read_first_json([fb_path, fb_fallback_path])
    tap_path = root / "target/qemu-eic7700-qtest-20261011/tap.txt"
    try:
        tap = tap_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        tap = ""

    checks: list[dict[str, str]] = []
    if recovery is None:
        checks.append(_check("software-recovery", "missing", "receipt-unreadable", str(recovery_path)))
        checks.append(_check("rockos-fallback", "missing", "receipt-unreadable", str(recovery_path)))
        checks.append(_check("automatic-receipts", "missing", "receipt-unreadable", str(recovery_path)))
    else:
        checks.append(_check(
            "software-recovery",
            "passed" if recovery.get("recovery_state") == "recovered" else "insufficient",
            "Asterinas-to-firmware recovery state",
            str(recovery_path),
        ))
        fallback = recovery.get("fallback_state")
        checks.append(_check(
            "rockos-fallback",
            "passed" if fallback == "rockos-login" else "missing",
            f"observed fallback={fallback!r}; U-Boot is not RockOS",
            str(recovery_path),
        ))
        checks.append(_check(
            "automatic-receipts",
            "passed"
            if recovery.get("serial_tail_sha256") and recovery.get("boot_id_state") == "observed"
            else "insufficient",
            "serial tail hash and boot identity",
            str(recovery_path),
        ))

    checks.append(_check(
        "framebuffer",
        "passed" if fb is not None and fb.get("passed") is True else "missing",
        "fb0 userspace transaction result",
        str(fb_evidence_path),
    ))
    checks.append(_check(
        "timer-disabled",
        "passed"
        if timer is not None
        and timer.get("classification") == "timer-disabled"
        and timer.get("qualified") is True
        else "missing",
        "guest timer fault receipt",
        str(timer_path),
    ))
    checks.append(_check(
        "sbi-reset-failure",
        "passed"
        if sbi is not None
        and sbi.get("classification") == "sbi-reset-failure"
        and sbi.get("classification_complete") is True
        else "missing",
        "guest SBI reset fault receipt",
        str(sbi_path),
    ))

    qtest_ok = len(_OK_LINE.findall(tap))
    qtest_required = (
        "fault-register-read-fail-closed" in tap
        and "mmio-timeout-fail-closed" in tap
        and qtest_ok >= 19
    )
    checks.append(_check(
        "gmac-dwc3-qtest",
        "passed" if qtest_required else "missing",
        f"QTest ok-lines={qtest_ok}; GMAC/DWC3 fault names present={qtest_required}",
        str(tap_path),
    ))

    guest_mmio = (
        fault_matrix is not None
        and fault_matrix.get("execution_scope") == "qemu-guest"
        and fault_matrix.get("classification_complete") is True
        and fault_matrix.get("qualified") is True
        and "mmio-permanent-block" in fault_matrix.get("faults", [])
    )
    checks.append(_check(
        "mmio-permanent-block",
        "passed" if guest_mmio else "insufficient",
        "requires a guest-scoped receipt; host-serial-peer is insufficient",
        str(fault_matrix_path),
    ))

    return {
        "schema": "asterinas.qemu-goal-audit.v1",
        "execution_scope": "host-evidence-audit",
        "hardware_state": "not-accessed",
        "passed": all(item["status"] == "passed" for item in checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    audit = build_goal_audit(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0 if audit["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
