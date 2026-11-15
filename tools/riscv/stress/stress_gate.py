#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Stress/concurrency gate: run the stress suite as PID 1 in QEMU.

The suite (tools/riscv/stress/stress_suite.c) forks/execs, creates threads,
hammers futex-backed mutexes and condvars, touches anonymous and shared
mappings, does fsync'd tmpfs I/O, drives 64 socketpairs through epoll and
ping-pongs SIGUSR1. The gate passes only when every sub-test reports PASS and
the kernel never panics.

Usage:
  tools/riscv/stress/stress_gate.py --output-dir target/stress/run-1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_KERNEL = REPO / "target/ltp/kernel-sv39.img"
BUSYBOX = REPO / "target/nixos/busybox"
CC = "riscv64-linux-musl-gcc"
GNU_LIB = "/usr/riscv64-linux-gnu/lib"
DONE_MARKER = "STRESS_DONE"
TIMEOUT_SECONDS = 600


class GateError(RuntimeError):
    pass


def run(args: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def build(build_dir: Path) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    suite = build_dir / "stress_suite"
    result = run([
        CC, "-O2", "-static", "-pthread", f"-L{GNU_LIB}", "-o", str(suite),
        str(REPO / "tools/riscv/stress/stress_suite.c"),
    ])
    if result.returncode != 0:
        raise GateError(f"stress suite build failed:\n{result.stderr.decode(errors='replace')[-800:]}")
    root = build_dir / "rootfs"
    if root.exists():
        shutil.rmtree(root)
    (root / "bin").mkdir(parents=True)
    (root / "tmp").mkdir()
    (root / "proc").mkdir()
    (root / "sys").mkdir()
    shutil.copy2(BUSYBOX, root / "bin/busybox")
    for applet in ("true", "sh", "ls", "cat"):
        (root / "bin" / applet).symlink_to("busybox")
    shutil.copy2(suite, root / "init")
    os.chmod(root / "init", 0o755)
    initramfs = build_dir / "stress-initramfs.cpio.gz"
    subprocess.run(
        ["sh", "-c", f"cd {root} && find . | cpio -o -H newc 2>/dev/null | gzip -9 > {initramfs}"],
        check=True,
    )
    return initramfs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL)
    args = parser.parse_args(argv)

    output = args.output_dir.resolve()
    if output.exists():
        parser.error(f"output directory must be new: {output}")
    output.mkdir(parents=True)
    if not args.kernel.is_file():
        raise SystemExit(f"missing kernel image: {args.kernel}")
    if not BUSYBOX.is_file():
        raise SystemExit(f"missing static busybox: {BUSYBOX}")

    initramfs = build(output / "build")
    serial = output / "serial.log"
    command = [
        "qemu-system-riscv64", "-machine", "virt", "-m", "2G", "-smp", "2",
        "-nographic", "-kernel", str(args.kernel), "-initrd", str(initramfs),
        "-append", "console=ttyS0 loglevel=warn init=/init",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    text = ""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            line = process.stdout.readline()  # type: ignore[union-attr]
            if not line:
                if process.poll() is not None:
                    break
                continue
            text += line.decode("utf-8", "replace")
            if DONE_MARKER in text:
                break
    finally:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError):
            process.kill()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    serial.write_text(text)

    verdicts = {}
    for line in text.replace("\r", "").splitlines():
        if line.startswith("STRESS ") and line.split()[2:3] in (["PASS"], ["FAIL"]):
            verdicts[line.split()[1]] = line.split()[2]
    failures = [name for name, verdict in verdicts.items() if verdict != "PASS"]
    panicked = "Uncaught panic" in text or "Printing stack trace" in text
    completed = DONE_MARKER in text
    result = {
        "status": "passed" if completed and not failures and not panicked else "failed",
        "completed": completed,
        "panicked": panicked,
        "verdicts": verdicts,
        "failures": failures,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for name, verdict in verdicts.items():
        print(f"{name}: {verdict}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
    if panicked:
        print("kernel panic detected in the serial log")
    if not completed:
        print("suite did not finish (timeout or crash)")
    print("stress gate: " + ("PASSED" if result["status"] == "passed" else "FAILED"))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
