"""Deterministic QEMU/U-Boot recovery fault injection and classification.

The faults in this module are host-side serial protocol injections.  They do
not touch a board or a real MMIO device; their purpose is to make the recovery
receipt distinguish a kernel hang from a missing SBI reset and from a boot
loader that never exposes a prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from qemu_uboot_commands import BootCommand
from qemu_uboot_session import RebootExpectation, SessionResult, run_serial_session


class RecoveryFault(str, Enum):
    """Faults whose containment and recovery evidence are tested."""

    TIMER_DISABLED = "timer-disabled"
    SBI_RESET_FAILURE = "sbi-reset-failure"
    MMIO_PERMANENT_BLOCK = "mmio-permanent-block"
    UBOOT_WAIT = "uboot-wait"


REBOOT_TIMER_READY = b"ASTERINAS_REBOOT_TIMER_READY"
_FAULT_RE = re.compile(rb"ASTERINAS_FAULT_INJECTED fault=([a-z0-9-]+)")
_BOOT_ID_RE = re.compile(
    rb"\bboot[_-]?id\s*[=:]\s*"
    rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)


class RecoveryFaultError(ValueError):
    """The serial result does not satisfy the selected fault contract."""


@dataclass(frozen=True)
class FaultRun:
    """One contained synthetic failure and its machine-readable receipt."""

    fault: RecoveryFault
    result: SessionResult
    serial: bytes
    receipt: Mapping[str, object]


def _expected_failure(fault: RecoveryFault) -> str:
    if fault is RecoveryFault.UBOOT_WAIT:
        return "startup-timeout"
    if fault is RecoveryFault.SBI_RESET_FAILURE:
        return "recovery-timeout:opensbi"
    return "boot-timeout"


def classify_fault(
    fault: RecoveryFault,
    result: SessionResult,
    serial: bytes,
) -> dict[str, object]:
    """Validate one injected fault and return a bounded recovery receipt."""

    observed = _FAULT_RE.findall(serial)
    expected_marker = fault.value.encode("ascii")
    if observed != [expected_marker]:
        raise RecoveryFaultError(
            f"expected exactly one {fault.value!r} fault marker; observed {observed!r}"
        )
    expected_failure = _expected_failure(fault)
    if result.failure != expected_failure:
        raise RecoveryFaultError(
            f"{fault.value} expected {expected_failure}, got {result.failure}"
        )
    if not result.timed_out:
        raise RecoveryFaultError(f"{fault.value} was not bounded by a timeout")
    if not result.cleanup_complete:
        raise RecoveryFaultError(f"{fault.value} left the process group alive")
    if result.recovery_complete:
        raise RecoveryFaultError(f"{fault.value} unexpectedly reported recovery")
    if result.booti_sent_count > 1:
        raise RecoveryFaultError(f"{fault.value} sent booti more than once")

    if fault is RecoveryFault.UBOOT_WAIT:
        classification = "uboot-wait"
    elif fault is RecoveryFault.SBI_RESET_FAILURE:
        classification = "sbi-reset-failure"
    elif fault is RecoveryFault.TIMER_DISABLED:
        classification = "kernel-hang:timer-disabled"
    else:
        classification = "kernel-hang:mmio-permanent-block"
    boot_ids: list[str] = []
    for match in _BOOT_ID_RE.findall(serial):
        boot_id = match.decode("ascii").lower()
        if boot_id not in boot_ids:
            boot_ids.append(boot_id)
    return {
        "schema": "asterinas.qemu-recovery-fault-receipt.v1",
        "fault": fault.value,
        "classification": classification,
        "failure": result.failure,
        "contained": result.cleanup_complete,
        # The current fault peer is deliberately a bounded host-side serial
        # process.  It does not execute the Asterinas kernel or arm a real
        # hardware watchdog; keeping this provenance in every receipt avoids
        # promoting a protocol test into a hardware claim.
        "execution_scope": "host-serial-peer",
        "watchdog_state": "not-modeled",
        "recovery_state": "not-recovered",
        "boot_ids": boot_ids,
        "booti_sent_count": result.booti_sent_count,
        "serial_bytes": len(serial),
        "serial_tail_sha256": hashlib.sha256(serial[-4096:]).hexdigest(),
        "serial_tail": serial[-4096:].decode("utf-8", errors="replace"),
    }


def _write_fake_qemu(path: Path, fault: RecoveryFault) -> None:
    """Write a tiny unbuffered serial peer for one fault scenario."""

    marker = fault.value
    if fault is RecoveryFault.UBOOT_WAIT:
        body = (
            "print('OpenSBI v1.7 boot_id=01234567-89ab-cdef-0123-456789abcdef', flush=True)\n"
            "print('U-Boot 2026.07', flush=True)\n"
            f"print('ASTERINAS_FAULT_INJECTED fault={marker}', flush=True)\n"
            "time.sleep(60)\n"
        )
    elif fault is RecoveryFault.SBI_RESET_FAILURE:
        body = (
            "print('OpenSBI v1.7 boot_id=01234567-89ab-cdef-0123-456789abcdef', flush=True)\n"
            "print('=> ', end='', flush=True)\n"
            "for line in sys.stdin:\n"
            " if line.startswith('booti '):\n"
            f"  print('ASTERINAS_FAULT_INJECTED fault={marker}', flush=True)\n"
            "  print('ASTERINAS_REBOOT_TIMER_READY', flush=True)\n"
            "  time.sleep(60)\n"
        )
    else:
        body = (
            "print('OpenSBI v1.7 boot_id=01234567-89ab-cdef-0123-456789abcdef', flush=True)\n"
            "print('=> ', end='', flush=True)\n"
            "for line in sys.stdin:\n"
            " if line.startswith('booti '):\n"
            f"  print('ASTERINAS_FAULT_INJECTED fault={marker}', flush=True)\n"
            "  time.sleep(60)\n"
        )
    path.write_text(
        "import sys\nimport time\n"
        "sys.stdout.reconfigure(line_buffering=True)\n"
        + body,
        encoding="utf-8",
    )


def run_fault(fault: RecoveryFault, directory: Path) -> FaultRun:
    """Run one synthetic fault with bounded timeouts and no retry."""

    fake = directory / f"{fault.value}.py"
    _write_fake_qemu(fake, fault)
    log_path = directory / f"{fault.value}.serial"
    kwargs: dict[str, object] = {
        "startup_timeout": 0.2,
        "command_timeout": 0.2,
        "boot_timeout": 0.25,
        "termination_grace": 0.2,
    }
    if fault is RecoveryFault.SBI_RESET_FAILURE:
        kwargs["reboot_expectation"] = RebootExpectation(
            trigger_marker=REBOOT_TIMER_READY,
            recovery_timeout=0.2,
            milestones=(("opensbi", b"OpenSBI v1.7"),),
        )
    else:
        kwargs["reboot_expectation"] = None
    result = run_serial_session(
        [sys.executable, "-u", str(fake)],
        commands=(BootCommand("booti", "booti image", "UNREACHABLE"),),
        raw_log_path=log_path,
        **kwargs,
    )
    serial = log_path.read_bytes()
    return FaultRun(fault, result, serial, classify_fault(fault, result, serial))


def run_fault_matrix() -> tuple[FaultRun, ...]:
    """Run every fault in an isolated temporary output directory."""

    with tempfile.TemporaryDirectory(prefix="asterinas-recovery-fault-") as root:
        directory = Path(root)
        return tuple(run_fault(fault, directory) for fault in RecoveryFault)


def build_fault_matrix_receipt(runs: Sequence[FaultRun]) -> dict[str, object]:
    """Build a non-qualifying receipt for the bounded host fault matrix.

    ``passed`` deliberately remains false: the matrix proves classification and
    cleanup, but this adapter does not arm a guest or hardware watchdog and
    therefore cannot prove recovery.  ``classification_complete`` is the
    positive result consumed by host tests and evidence tooling.
    """

    items = [dict(run.receipt) for run in runs]
    scopes = {item.get("execution_scope") for item in items}
    watchdog_states = {item.get("watchdog_state") for item in items}
    if scopes != {"host-serial-peer"}:
        raise RecoveryFaultError(f"unexpected fault scopes: {sorted(scopes)!r}")
    if watchdog_states != {"not-modeled"}:
        raise RecoveryFaultError(
            f"unexpected watchdog states: {sorted(watchdog_states)!r}"
        )
    return {
        "schema": "asterinas.qemu-recovery-fault-matrix-receipt.v1",
        "execution_scope": "host-serial-peer",
        "hardware_state": "not-accessed",
        "watchdog_state": "not-modeled",
        "recovery_state": "not-recovered",
        "passed": False,
        "classification_complete": True,
        "fault_count": len(items),
        "faults": items,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="run the bounded host-side recovery fault matrix"
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        receipt = build_fault_matrix_receipt(run_fault_matrix())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, RecoveryFaultError) as error:
        print(f"qemu recovery fault matrix: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
