#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run a frozen, network-free kernel-log micro guest and retain its evidence."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time


REQUIRED_CASES = (
    "action_validation",
    "independent_readers_and_priority",
    "record_reads_and_seeks",
    "clear_preserves_device_records",
    "destructive_cursor_is_separate",
    "read_clear_fault_boundary",
    "overwritten_cursor",
    "blocking_wait_and_signal",
    "capture_level_retains_info",
    "restricted_readers",
)

BOOT_APPEND = (
    "init=/init loglevel=off asterinas.klog_capture=info "
    "asterinas.syscall_diag=1 console=ttyS0"
)


def validate(transcript):
    """Reject incomplete probes, hidden failures, or fatal output after success."""
    lines = transcript.splitlines()
    markers = (
        "KLOG_MICRO_BEGIN",
        "KLOG_CAPTURE_INFO_RETAINED=1",
        "KLOG_MICRO_PROBE_EXIT=0",
        "KLOG_CONSOLE_BEGIN",
        "KLOG_CONSOLE_END",
        "KLOG_CONSOLE_CAPTURE kernel=1 hidden=1 visible=1",
        "KLOG_CONSOLE_EXIT=0",
        "KLOG_DMESG_BEGIN",
        "KLOG_DMESG_EXIT=0",
        "KLOG_UTIL_DMESG_EXIT=0",
        "KLOG_DMESG_FOLLOW_CHILD before_cleanup=running status=0",
        "KLOG_FOLLOW_EXIT=0",
        "KLOG_MICRO_END",
    )
    positions = []
    for marker in markers:
        if lines.count(marker) != 1:
            raise ValueError(f"expected exactly one success marker: {marker}")
        if "=" in marker:
            family = marker.split("=", 1)[0] + "="
            if sum(line.startswith(family) for line in lines) != 1:
                raise ValueError(f"conflicting or duplicate status: {family}")
        positions.append(lines.index(marker))
    if positions != sorted(positions):
        raise ValueError("probe markers are out of order")
    if re.search(
        r"uncaught panic|kernel panic|unexpected exception|stack trace:",
        transcript,
        re.IGNORECASE,
    ):
        raise ValueError("fatal output in complete transcript")
    if re.search(r"[1-9][0-9]* tests failed", transcript):
        raise ValueError("a C assertion failed")
    if "KLOG_DMESG_FOLLOW checkpoint=old_read_limit" in transcript:
        raise ValueError("dmesg --follow-new unexpectedly drained the old buffer")
    count = 0
    case_begin = lines.index("KLOG_MICRO_BEGIN")
    case_end = lines.index("KLOG_MICRO_PROBE_EXIT=0")
    for case in REQUIRED_CASES:
        summaries = re.findall(
            rf"^test_{case} summary: ([1-9][0-9]*) tests passed, 0 tests failed$",
            "\n".join(lines),
            re.MULTILINE,
        )
        if len(summaries) != 1:
            raise ValueError(f"missing or duplicate C case: {case}")
        family = [
            index
            for index, line in enumerate(lines)
            if line.startswith(f"test_{case} summary:")
        ]
        if len(family) != 1:
            raise ValueError(f"conflicting or duplicate C summary: {case}")
        position = family[0]
        if not case_begin < position < case_end:
            raise ValueError(f"C evidence is outside its phase: {case}")
        count += int(summaries[0])
    follow_position = lines.index("KLOG_UTIL_DMESG_EXIT=0")
    follow_end = lines.index("KLOG_DMESG_FOLLOW_CHILD before_cleanup=running status=0")
    for stage in (0, 1):
        prefix = f"KLOG_DMESG_FOLLOW stage={stage} observed=1 "
        if sum(line.startswith(prefix) for line in lines) != 1:
            raise ValueError(f"dmesg follow did not acknowledge stage {stage}")
        family = f"KLOG_DMESG_FOLLOW stage={stage} "
        if sum(line.startswith(family) for line in lines) != 1:
            raise ValueError(f"conflicting follow result for stage {stage}")
        position = next(
            index for index, line in enumerate(lines) if line.startswith(prefix)
        )
        if not follow_position < position < follow_end:
            raise ValueError(f"follow evidence is outside its ordered phase: {stage}")
        follow_position = position
    console = "\n".join(
        lines[lines.index("KLOG_CONSOLE_BEGIN") + 1 : lines.index("KLOG_CONSOLE_END")]
    )
    if "aster-klog-console-visible" not in console:
        raise ValueError("user log was not emitted to the enabled console")
    if (
        "aster-klog-console-hidden" in console
        or "Unimplemented syscall number:" in console
    ):
        raise ValueError("a suppressed record leaked to the console")
    console_position = lines.index("KLOG_CONSOLE_BEGIN")
    for phase, message in (
        ("visible", "aster-klog-console-visible"),
        ("reenabled", "aster-klog-console-visible-reenabled"),
    ):
        begin = f"KLOG_CONSOLE_PHASE {phase}_begin"
        end = f"KLOG_CONSOLE_PHASE {phase}_end retained_user=1"
        if lines.count(begin) != 1 or lines.count(end) != 1:
            raise ValueError(f"missing or duplicate console phase: {phase}")
        start, stop = lines.index(begin), lines.index(end)
        if not console_position < start < stop < lines.index("KLOG_CONSOLE_END"):
            raise ValueError(f"console phase is out of order: {phase}")
        output = [
            re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines[start + 1 : stop]
        ]
        if not any(line.endswith(": " + message) for line in output):
            raise ValueError(f"expected console record is missing: {message}")
        console_position = stop
    lifecycle = "syscall_diag lifecycle=clone"
    dmesg_begin = lines.index("KLOG_DMESG_BEGIN")
    lifecycle_positions = [
        index for index, line in enumerate(lines) if lifecycle in line
    ]
    if not lifecycle_positions:
        raise ValueError("informational lifecycle record is absent from dmesg")
    if any(position <= dmesg_begin for position in lifecycle_positions):
        raise ValueError("informational lifecycle record leaked outside dmesg")
    return count


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument(
        "--out", type=Path, required=True, help="new evidence directory"
    )
    parser.add_argument("--qemu", default="qemu-system-riscv64")
    parser.add_argument("--smp", type=int, choices=(1, 4), default=4)
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    qemu = shutil.which(args.qemu)
    if qemu is None:
        parser.error("cached QEMU is required; this gate never installs tools")
    inputs = {}
    for key, path in (("kernel", args.kernel), ("initramfs", args.initramfs)):
        if not path.is_file() or path.stat().st_size == 0:
            parser.error(f"{key} must be a nonempty existing file")
        inputs[key] = {"path": str(path.resolve()), "sha256": digest(path)}
    # Never replace a previous experiment's inputs, transcript, or verdict.
    args.out.mkdir(parents=True, exist_ok=False)
    command = [
        qemu,
        "-machine",
        "virt",
        "-cpu",
        "rv64,svpbmt=true,zkr=false",
        "-m",
        "2G",
        "-smp",
        str(args.smp),
        "-nographic",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-bios",
        "default",
        "-kernel",
        inputs["kernel"]["path"],
        "-initrd",
        inputs["initramfs"]["path"],
        "-append",
        BOOT_APPEND,
    ]
    result = {"physical": False, "passed": False, "inputs": inputs, "command": command}
    started = time.monotonic()
    serial = args.out / "serial.log"
    try:
        with serial.open("wb") as log:
            process = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
        result["returncode"] = process.returncode
        if process.returncode != 0:
            raise ValueError(f"QEMU exited with {process.returncode}")
        result["c_assertions_passed"] = validate(serial.read_text(errors="replace"))
        for key, entry in inputs.items():
            if digest(Path(entry["path"])) != entry["sha256"]:
                raise ValueError(f"{key} changed during the experiment")
        result["passed"] = True
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
