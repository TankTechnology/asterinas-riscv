#!/usr/bin/env python3
"""Classify bounded fault boundaries observed from an actual QEMU guest.

This parser consumes the guarded runner's ``result.json`` and serial log.  It
does not start QEMU or touch hardware.  It supports SBI reset failure and the
post-handoff timer-disabled boundary; both require a single firmware epoch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence


RECOVERY_MARKER = "ASTERINAS_REBOOT_TIMER_READY"
ARMED_MARKER = "ASTERINAS_SOFTWARE_REBOOT_ARMED"
_TAIL_BYTES = 4096
_BOOT_ID_RE = re.compile(
    r"\bboot[_-]?id\s*[=:]\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)


class GuestFaultReceiptError(ValueError):
    """The guest transcript does not satisfy the selected fault contract."""


def build_guest_fault_receipt(
    result: Mapping[str, object],
    serial_text: str,
    *,
    fault: str = "sbi-reset-failure",
) -> dict[str, object]:
    """Validate one bounded QEMU guest fault boundary."""

    if fault not in {"sbi-reset-failure", "timer-disabled"}:
        raise GuestFaultReceiptError(f"unsupported guest fault: {fault!r}")
    argv = result.get("qemu_argv")
    if not isinstance(argv, list) or "-no-reboot" not in argv:
        raise GuestFaultReceiptError("guest reset-failure run must use -no-reboot")
    session = result.get("session")
    if not isinstance(session, Mapping):
        raise GuestFaultReceiptError("runner result has no session object")
    if session.get("cleanup_complete") is not True:
        raise GuestFaultReceiptError("QEMU process cleanup is incomplete")
    if session.get("recovery_complete") is not False:
        raise GuestFaultReceiptError("guest unexpectedly reported recovery")
    if serial_text.count(ARMED_MARKER) != 1:
        raise GuestFaultReceiptError("software recovery timer marker count is not one")
    if serial_text.count(RECOVERY_MARKER) != 1:
        raise GuestFaultReceiptError("recovery marker count is not one")
    opensbi_epochs = serial_text.count("OpenSBI v")
    uboot_banner_count = serial_text.count("U-Boot ")
    # U-Boot prints its version once during the banner and may print it again
    # when the guarded command sequence asks for ``version``.  OpenSBI is the
    # epoch boundary; require one U-Boot banner, but do not mistake the second
    # version line for a second firmware reset.
    uboot_epochs = 1 if uboot_banner_count >= 1 else 0
    if opensbi_epochs != 1 or uboot_epochs != 1:
        raise GuestFaultReceiptError(
            f"expected one firmware epoch, got OpenSBI={opensbi_epochs}, U-Boot={uboot_banner_count}"
        )
    qualified = False
    watchdog_state = "guest-software-timer-armed"
    if fault == "sbi-reset-failure":
        if session.get("returncode") != 0:
            raise GuestFaultReceiptError("QEMU did not exit with the expected code")
    else:
        machine_args = [
            str(argv[index + 1])
            for index, value in enumerate(argv[:-1])
            if value == "-machine"
        ]
        if not any("timer-fault-after-boot=on" in value for value in machine_args):
            raise GuestFaultReceiptError(
                "timer-disabled fault must be armed after the booti handoff"
            )
        observation_seconds = result.get("post_terminal_timeout_seconds")
        if (
            not isinstance(observation_seconds, (int, float))
            or observation_seconds <= 0
        ):
            raise GuestFaultReceiptError(
                "timer-disabled fault must record a positive post-terminal observation"
            )
        bounded_stop = session.get("timed_out") is True or (
            session.get("termination_action") == "SIGTERM"
            and session.get("returncode") == 0
        )
        if session.get("marker_seen") is not True or not bounded_stop:
            raise GuestFaultReceiptError(
                "timer-disabled fault must reach the guest marker and bounded timeout"
            )
        qualified = True
        watchdog_state = "guest-software-timer-stalled"
    serial_bytes = serial_text.encode("utf-8", errors="replace")
    tail = serial_bytes[-_TAIL_BYTES:]
    boot_ids: list[str] = []
    for match in _BOOT_ID_RE.finditer(serial_text):
        value = match.group(1).lower()
        if value not in boot_ids:
            boot_ids.append(value)
    return {
        "schema": "asterinas.qemu-guest-fault-receipt.v1",
        "execution_scope": "qemu-guest",
        "hardware_state": "not-accessed",
        "fault": fault,
        "classification": fault,
        "classification_complete": True,
        "qualified": qualified,
        "recovery_state": "not-recovered",
        "watchdog_state": watchdog_state,
        "watchdog_kind": "software-recovery-timer",
        "opensbi_epochs": opensbi_epochs,
        "uboot_epochs": uboot_epochs,
        "uboot_banner_count": uboot_banner_count,
        "boot_ids": boot_ids,
        "serial_bytes": len(serial_bytes),
        "serial_tail_sha256": hashlib.sha256(tail).hexdigest(),
        "serial_tail": tail.decode("utf-8", errors="replace"),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--serial-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fault", default="sbi-reset-failure")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = json.loads(args.result.read_text(encoding="utf-8"))
        serial_text = args.serial_log.read_text(encoding="utf-8", errors="replace")
        receipt = build_guest_fault_receipt(result, serial_text, fault=args.fault)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError, GuestFaultReceiptError) as error:
        print(f"qemu guest fault receipt: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
