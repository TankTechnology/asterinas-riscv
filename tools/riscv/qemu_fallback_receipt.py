#!/usr/bin/env python3
"""Create a bounded receipt for the second-boot QEMU fallback boundary.

This tool only parses a captured serial transcript.  It does not start QEMU,
write a serial device, or access hardware.  The recovery marker is required
before any U-Boot prompt or RockOS login can be considered a fallback; an
initial firmware prompt is deliberately classified as unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Sequence


RECOVERY_MARKER = "ASTERINAS_REBOOT_TIMER_READY"
LINUX_REFERENCE_MARKER = "ASTERINAS_LINUX_REFERENCE_READY"
_ROCKOS_LOGIN_PATTERN = re.compile(r"(?mi)^[ \t]*rockos-eswin login:[ \t]*$")
_BOOT_ID_PATTERN = re.compile(
    r"\bboot[_-]?id\s*[=:]\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)
_TAIL_BYTES = 4096


class FallbackKind(str, Enum):
    """Fallback payloads that have an independently observable terminal."""

    LINUX_REFERENCE = "linux-reference"
    ROCKOS_LOGIN = "rockos-login"


class FallbackReceiptError(ValueError):
    """The requested fallback contract is not supported."""


def _coerce_kind(value: FallbackKind | str) -> FallbackKind:
    try:
        return value if isinstance(value, FallbackKind) else FallbackKind(value)
    except ValueError as error:
        raise FallbackReceiptError(f"unsupported fallback kind: {value!r}") from error


def _post_recovery(serial_text: str) -> tuple[str, int]:
    count = serial_text.count(RECOVERY_MARKER)
    if count == 0:
        return "", 0
    position = serial_text.find(RECOVERY_MARKER)
    return serial_text[position + len(RECOVERY_MARKER) :], count


def _observed_fallback(post_recovery: str) -> str:
    linux = post_recovery.count(LINUX_REFERENCE_MARKER)
    rockos = _ROCKOS_LOGIN_PATTERN.search(post_recovery) is not None
    if linux and rockos:
        return "mixed"
    if linux:
        return "linux-reference"
    if rockos:
        return "rockos-login"
    if "=> " in post_recovery:
        return "uboot-prompt"
    return "unknown"


def build_fallback_receipt(
    serial_text: str,
    *,
    fallback_kind: FallbackKind | str,
) -> dict[str, object]:
    """Classify one serial transcript without upgrading ambiguous evidence."""

    expected = _coerce_kind(fallback_kind)
    post_recovery, recovery_count = _post_recovery(serial_text)
    observed = _observed_fallback(post_recovery)
    boot_ids: list[str] = []
    for match in _BOOT_ID_PATTERN.finditer(serial_text):
        boot_id = match.group(1).lower()
        if boot_id not in boot_ids:
            boot_ids.append(boot_id)

    failure: str | None = None
    if recovery_count == 0:
        failure = "recovery-marker-missing"
    elif recovery_count != 1:
        failure = "recovery-marker-count"
    elif serial_text.count(LINUX_REFERENCE_MARKER) > 1:
        failure = "fallback-marker-count"
    elif expected is FallbackKind.LINUX_REFERENCE:
        if serial_text.count(LINUX_REFERENCE_MARKER) != 1:
            failure = "fallback-marker-missing"
        elif observed != expected.value:
            failure = "fallback-kind-mismatch"
    elif observed != expected.value:
        failure = "fallback-kind-mismatch"

    if failure is None and serial_text.count("OpenSBI v") < 2:
        failure = "opensbi-epoch-missing"
    if failure is None and serial_text.count("U-Boot ") < 2:
        failure = "uboot-epoch-missing"

    passed = failure is None
    if passed:
        recovery_state = "recovered-to-fallback"
    elif recovery_count:
        recovery_state = "recovery-observed-fallback-missing"
    else:
        recovery_state = "not-recovered"
    serial_bytes = serial_text.encode("utf-8", errors="replace")
    tail = serial_bytes[-_TAIL_BYTES:]
    return {
        "schema": "asterinas.qemu-fallback-receipt.v1",
        "execution_scope": "qemu-guest",
        "hardware_state": "not-accessed",
        "expected_fallback": expected.value,
        "observed_fallback": observed,
        "passed": passed,
        "failure": failure,
        "recovery_state": recovery_state,
        "watchdog_state": "armed" if recovery_count else "missing",
        "watchdog_kind": (
            "software-recovery-timer" if recovery_count else "not-observed"
        ),
        "recovery_marker_count": recovery_count,
        "linux_reference_marker_count": serial_text.count(LINUX_REFERENCE_MARKER),
        "boot_ids": boot_ids,
        "boot_id_state": "observed" if boot_ids else "missing",
        "opensbi_epochs": serial_text.count("OpenSBI v"),
        "uboot_epochs": serial_text.count("U-Boot "),
        "serial_bytes": len(serial_bytes),
        "serial_tail_sha256": hashlib.sha256(tail).hexdigest(),
        "serial_tail": tail.decode("utf-8", errors="replace"),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--fallback-kind",
        choices=tuple(kind.value for kind in FallbackKind),
        required=True,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        serial_text = args.serial_log.read_text(encoding="utf-8", errors="replace")
        receipt = build_fallback_receipt(
            serial_text,
            fallback_kind=args.fallback_kind,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, FallbackReceiptError) as error:
        print(f"qemu fallback receipt: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
