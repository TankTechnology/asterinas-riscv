#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Exercise the ext4 extent and JBD2 paths in QEMU.

The gate boots a supplied ext4 image with the rebased RISC-V kernel and runs
two independent suites:

* a persistent write suite covering directories, ordinary and fragmented
  extent files, truncate, hard links, rename, and fsync;
* one crash/replay pass for each deterministic JBD2 fault stage.  The image is
  copied for every pass, so a stage can only affect its own copy.

Every pass ends with ``e2fsck -fn``.  The ext4 image must be built with the
compatibility feature set (``extent``, ``has_journal``, no metadata checksum,
no flex_bg/orphan_file/64-bit high block numbers).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from direct_boot_gate import (  # noqa: E402
    ANSI,
    PROMPT,
    GateError,
    Serial,
    build_qemu_argv,
)

GATE_BEGIN = "GATE_EXT4_BEGIN"
SEMANTICS_END = "GATE_EXT4_SEMANTICS_END"
SYNC_DONE = "GATE_EXT4_SYNC_DONE"
RECOVERY_END = "GATE_EXT4_RECOVERY_END"

COMMON_COMMANDS = [
    "mount -t proc proc /proc 2>/dev/null || true",
    "mount -t sysfs sysfs /sys 2>/dev/null || true",
    "mount -t devtmpfs devtmpfs /dev 2>/dev/null || true",
    "export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
]

SEMANTICS_COMMANDS = [
    "rm -rf /ext4-gate",
    "mkdir -p /ext4-gate/dir",
    "echo hello-ext4 > /ext4-gate/dir/a.txt",
    "dd if=/dev/zero of=/ext4-gate/dir/data.bin bs=4096 count=256 2>/dev/null && echo GATE_DD_OK",
    "for n in 0 64 128 192 256 320 384 448 512 576 640 704; do "
    "dd if=/dev/zero of=/ext4-gate/sparse.bin bs=4096 count=1 seek=$n "
    "2>/dev/null || exit 1; done; echo GATE_EXTENTS_OK",
    "dd if=/dev/zero of=/ext4-gate/large.bin bs=4096 count=2048 2>/dev/null && echo GATE_LARGE_OK",
    "dd if=/dev/urandom of=/ext4-gate/rand.bin bs=4096 count=64 "
    "2>/dev/null && echo GATE_RAND_WRITE_OK",
    "sha256sum /ext4-gate/rand.bin > /ext4-gate/rand.sha && sync && "
    "sha256sum -c /ext4-gate/rand.sha && echo GATE_RAND_SHA_OK",
    "truncate -s 1048576 /ext4-gate/large.bin && echo GATE_TRUNC_PARTIAL_OK",
    "truncate -s 0 /ext4-gate/sparse.bin && echo GATE_TRUNC_ZERO_OK",
    "ln /ext4-gate/dir/a.txt /ext4-gate/hardlink.txt && echo GATE_LINK_OK",
    "sha256sum /ext4-gate/dir/a.txt /ext4-gate/hardlink.txt",
    "sync && echo GATE_SYNC_OK",
    "rm /ext4-gate/hardlink.txt && echo GATE_UNLINK_OK",
    "mv /ext4-gate/dir/a.txt /ext4-gate/moved.txt && echo GATE_RENAME_OK",
    "cat /ext4-gate/moved.txt",
    "sync && echo GATE_SYNC2_OK",
    "mkdir -p /ext4-gate/many; n=0; while [ $n -lt 200 ]; do "
    "printf 'file-%s\\n' \"$n\" > /ext4-gate/many/f$n; n=$((n + 1)); done; "
    "echo GATE_MANY_CREATED",
    "count=0; for f in /ext4-gate/many/f*; do "
    "[ -f \"$f\" ] && count=$((count + 1)); done; "
    "[ \"$count\" -eq 200 ] && echo GATE_MANY_COUNT_OK",
    "sync && echo GATE_MANY_SYNC_OK",
    "path=/ext4-gate/deep; n=0; while [ $n -lt 20 ]; do "
    "path=$path/d$n; mkdir -p \"$path\"; n=$((n + 1)); done; "
    "echo GATE_DEEP_CREATED",
    "dd if=/dev/zero of=/ext4-gate/sparse-read.bin bs=4096 count=1 seek=256 "
    "2>/dev/null; sync; dd if=/ext4-gate/sparse-read.bin bs=4096 skip=128 count=1 "
    "2>/dev/null | od -An -v -tx1 | tr -d ' \\n' | grep -q '^0*$' && "
    "echo GATE_HOLE_ZERO_OK",
    "fallocate -l 262144 /ext4-gate/falloc.bin && echo GATE_FALLOCATE_ALLOCATE_OK",
    "fallocate -z -o 4096 -l 8192 /ext4-gate/falloc.bin && echo GATE_FALLOCATE_ZERO_OK",
    "dd if=/ext4-gate/falloc.bin bs=4096 skip=1 count=2 2>/dev/null | "
    "od -An -v -tx1 | tr -d ' \\n' | grep -q '^0*$' && "
    "echo GATE_FALLOCATE_ZERO_VERIFY_OK",
    "sync && echo GATE_FALLOCATE_SYNC_OK",
    "echo GATE_EXT4_SEMANTICS_END",
]

SEMANTICS_MARKERS = (
    "GATE_DD_OK",
    "GATE_EXTENTS_OK",
    "GATE_LARGE_OK",
    "GATE_RAND_WRITE_OK",
    "GATE_RAND_SHA_OK",
    "GATE_TRUNC_PARTIAL_OK",
    "GATE_TRUNC_ZERO_OK",
    "GATE_LINK_OK",
    "GATE_SYNC_OK",
    "GATE_UNLINK_OK",
    "GATE_RENAME_OK",
    "GATE_SYNC2_OK",
    "GATE_MANY_CREATED",
    "GATE_MANY_COUNT_OK",
    "GATE_MANY_SYNC_OK",
    "GATE_DEEP_CREATED",
    "GATE_HOLE_ZERO_OK",
    "GATE_FALLOCATE_ALLOCATE_OK",
    "GATE_FALLOCATE_ZERO_OK",
    "GATE_FALLOCATE_ZERO_VERIFY_OK",
    "GATE_FALLOCATE_SYNC_OK",
)

CRASH_COMMANDS = [
    "mkdir -p /ext4-journal/many",
    "n=0; while [ $n -lt 32 ]; do echo file-$n > /ext4-journal/many/f$n; "
    "n=$((n + 1)); done; echo GATE_EXT4_MANY_CREATED",
    "echo journal-payload > /ext4-journal/marker.txt",
    "sync; echo GATE_EXT4_SYNC_DONE",
]

RECOVERY_MANY = (
    "count=0; for f in /ext4-journal/many/f*; do "
    "[ -f \"$f\" ] && count=$((count + 1)); done; "
    "echo GATE_EXT4_MANY=$count"
)


class Ext4GateError(RuntimeError):
    """The ext4 gate did not reach the expected state."""


def copy_sparse(source: Path, destination: Path) -> None:
    """Copies an image while preserving holes."""
    subprocess.run(
        ["cp", "--sparse=always", str(source), str(destination)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def start_qemu(
    args: argparse.Namespace,
    image: Path,
    log: Path,
    *,
    extra_kernel_args: str | None = None,
) -> tuple[subprocess.Popen[bytes], Serial]:
    argv = build_qemu_argv(
        qemu=shutil.which(args.qemu) or args.qemu,
        kernel=args.kernel,
        rootfs=image,
        dsh_image=None,
        memory=args.memory,
        smp=args.smp,
        rootfstype="ext4",
        extra_kernel_args=extra_kernel_args,
        snapshot=False,
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, Serial(process, log)


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def send_commands(serial: Serial, commands: list[str]) -> None:
    for command in commands:
        serial.send(command)


def expect_prompt(serial: Serial, timeout: float) -> None:
    serial.expect(PROMPT, timeout)


def wait_for_exit_or_sync(
    process: subprocess.Popen[bytes], serial: Serial, timeout: float
) -> bool:
    """Returns true if the process exited, false if the sync returned."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        try:
            chunk = serial._read(0.5)
        except GateError:
            return True
        if SYNC_DONE in chunk:
            return False
    raise Ext4GateError("timed out waiting for the journal fault or sync completion")


def poweroff(process: subprocess.Popen[bytes], serial: Serial) -> None:
    try:
        serial.send("poweroff -f")
    except (BrokenPipeError, OSError):
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        stop_process(process)


def collect_reports(work_dir: Path, log_dir: Path) -> None:
    for pattern in ("*.log", "e2fsck-*.txt"):
        for report in work_dir.glob(pattern):
            shutil.copy2(report, log_dir / report.name)


def run_e2fsck(image: Path, log: Path) -> None:
    result = subprocess.run(
        ["e2fsck", "-fn", str(image)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        raise Ext4GateError(
            f"e2fsck -fn failed with {result.returncode}; see {log}"
        )


def run_semantics(args: argparse.Namespace, image: Path, log_dir: Path) -> None:
    log = log_dir / "semantics.log"
    process, serial = start_qemu(args, image, log)
    try:
        expect_prompt(serial, args.timeout)
        send_commands(serial, COMMON_COMMANDS)
        serial.send(f"echo {GATE_BEGIN}")
        send_commands(serial, SEMANTICS_COMMANDS)
        output = ANSI.sub("", serial.expect(re.compile(rf"(?m)^{SEMANTICS_END}\r?$"), args.timeout))
        missing = [marker for marker in SEMANTICS_MARKERS if marker not in output]
        if missing:
            raise Ext4GateError(f"semantics markers missing: {', '.join(missing)}")
        if "hello-ext4" not in output:
            raise Ext4GateError("moved file content was not readable")
        poweroff(process, serial)
    finally:
        serial.close()
        stop_process(process)
    run_e2fsck(image, log_dir / "e2fsck-semantics.txt")


def run_crash_stage(
    args: argparse.Namespace, base_image: Path, log_dir: Path, stage: int
) -> str:
    image = log_dir / f"journal-stage{stage}.ext4"
    copy_sparse(base_image, image)

    crash_log = log_dir / f"crash-stage{stage}.log"
    process, serial = start_qemu(
        args,
        image,
        crash_log,
        extra_kernel_args=f"asterinas.ext4_fault_stage={stage}",
    )
    try:
        expect_prompt(serial, args.timeout)
        send_commands(serial, COMMON_COMMANDS)
        send_commands(serial, CRASH_COMMANDS)
        crashed = wait_for_exit_or_sync(process, serial, args.timeout)
        if not crashed:
            raise Ext4GateError(
                f"journal fault stage {stage} did not stop the guest"
            )
        if f"ASTERINAS_EXT4_FAULT_STOP stage={stage}" not in serial.buffer:
            raise Ext4GateError(
                f"journal fault stage {stage} exited without its marker"
            )
    finally:
        serial.close()
        stop_process(process)

    recovery_log = log_dir / f"recovery-stage{stage}.log"
    process, serial = start_qemu(args, image, recovery_log)
    try:
        expect_prompt(serial, args.timeout)
        send_commands(serial, COMMON_COMMANDS)
        serial.send("echo GATE_EXT4_RECOVERY_BEGIN")
        serial.send("cat /ext4-journal/marker.txt 2>/dev/null || echo GATE_EXT4_MARKER_MISSING")
        serial.send(RECOVERY_MANY)
        serial.send("sync && echo GATE_EXT4_RECOVERY_SYNC_OK")
        serial.send("echo GATE_EXT4_RECOVERY_END")
        output = ANSI.sub(
            "",
            serial.expect(re.compile(rf"(?m)^{RECOVERY_END}\r?$"), args.timeout),
        )
        if "GATE_EXT4_RECOVERY_SYNC_OK" not in output:
            raise Ext4GateError(f"stage {stage} post-recovery sync failed")
        recovered = "journal-payload" in output
        if stage == 1 and recovered:
            raise Ext4GateError("stage 1 replayed an uncommitted transaction")
        if stage != 1 and not recovered:
            raise Ext4GateError(f"stage {stage} did not recover the committed transaction")
        expected_many = 0 if stage == 1 else 32
        if f"GATE_EXT4_MANY={expected_many}" not in output:
            raise Ext4GateError(
                f"stage {stage} recovered an unexpected number of directory entries"
            )
        poweroff(process, serial)
    finally:
        serial.close()
        stop_process(process)

    run_e2fsck(image, log_dir / f"e2fsck-stage{stage}.txt")
    return "discarded" if stage == 1 else "recovered"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, default=Path("target/riscv-debian-ext4"))
    parser.add_argument("--qemu", default="qemu-system-riscv64")
    parser.add_argument("--memory", default="2G")
    parser.add_argument("--smp", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--fault-stages",
        default="1,2,3,4",
        help="comma-separated journal crash stages, or an empty string to skip",
    )
    parser.add_argument("--keep-images", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.kernel.is_file():
        print(f"kernel not found: {args.kernel}", file=sys.stderr)
        return 2
    if not args.rootfs.is_file():
        print(f"rootfs not found: {args.rootfs}", file=sys.stderr)
        return 2
    qemu = shutil.which(args.qemu)
    if qemu is None:
        print(f"QEMU executable not found: {args.qemu}", file=sys.stderr)
        return 2
    args.qemu = qemu

    args.log_dir.mkdir(parents=True, exist_ok=True)
    work_parent = args.log_dir if args.keep_images else None
    with tempfile.TemporaryDirectory(
        prefix="ext4-work-", dir=work_parent, delete=not args.keep_images
    ) as temporary:
        work_dir = Path(temporary)
        semantics_image = work_dir / "semantics.ext4"
        copy_sparse(args.rootfs, semantics_image)
        try:
            run_semantics(args, semantics_image, work_dir)
            stages = {}
            for item in filter(None, (part.strip() for part in args.fault_stages.split(","))):
                stage = int(item)
                if stage not in (1, 2, 3, 4):
                    raise Ext4GateError(f"invalid journal fault stage: {stage}")
                stages[stage] = run_crash_stage(args, args.rootfs, work_dir, stage)
            # Move the prose logs out of the temporary image directory.
            for log in work_dir.glob("*.log"):
                shutil.move(str(log), str(args.log_dir / log.name))
            for log in work_dir.glob("e2fsck-*.txt"):
                shutil.move(str(log), str(args.log_dir / log.name))
        except (Ext4GateError, GateError) as error:
            collect_reports(work_dir, args.log_dir)
            print(json.dumps({"status": "fail", "error": str(error)}, indent=2))
            return 1

    print(
        json.dumps(
            {
                "status": "pass",
                "rootfs": str(args.rootfs),
                "fault_stages": stages,
                "log_dir": str(args.log_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
