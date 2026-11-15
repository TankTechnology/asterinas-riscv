#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""LMBench comparison gate: Asterinas vs a Linux baseline in the same QEMU.

Both systems boot the same static-musl initramfs (busybox + LMBench subset +
bench_runner). The runner isolates every case with a 30 s watchdog, so a hang
in one benchmark does not lose the rest. Numbers are the benchmark's own
microseconds (or MB/s) per operation; the report also prints the
Asterinas/Linux ratio per case.

Usage:
  tools/riscv/perf/bench_gate.py \
      --asterinas target/ltp/kernel-sv39.img \
      --linux target/perf/linux/root/usr/lib/modules/*/vmlinuz \
      --output-dir target/perf/run-1 [--repetitions 1]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUSYBOX = REPO / "target/nixos/busybox"
LMBENCH_BIN = REPO / "target/perf/lmbench-subset"
CC = "riscv64-linux-musl-gcc"
GNU_LIB = "/usr/riscv64-linux-gnu/lib"
TIMEOUT_SECONDS = 900

# Each benchmark's own output -> a (case, value, unit, direction) tuple.
# `lower` is better for latency, `higher` is better for bandwidth.
PATTERNS = [
    (re.compile(r"^Simple syscall:\s*([0-9.]+) microseconds"), "syscall_null", "us", "lower"),
    (re.compile(r"^Simple read:\s*([0-9.]+) microseconds"), "syscall_read", "us", "lower"),
    (re.compile(r"^Simple write:\s*([0-9.]+) microseconds"), "syscall_write", "us", "lower"),
    (re.compile(r"^Simple stat:\s*([0-9.]+) microseconds"), "syscall_stat", "us", "lower"),
    (re.compile(r"^Simple fstat:\s*([0-9.]+) microseconds"), "syscall_fstat", "us", "lower"),
    (re.compile(r"^Simple open/close:\s*([0-9.]+) microseconds"), "syscall_open", "us", "lower"),
    (re.compile(r"^Pipe latency:\s*([0-9.]+) microseconds"), "pipe_latency", "us", "lower"),
    (re.compile(r"^Process fork\+exit:\s*([0-9.]+) microseconds"), "fork_exec", "us", "lower"),
    (re.compile(r"^Process fork\+execve:\s*([0-9.]+) microseconds"), "exec", "us", "lower"),
    (re.compile(r"^Process fork\+/bin/sh -c:\s*([0-9.]+) microseconds"), "shell", "us", "lower"),
    (re.compile(r"^Pipe bandwidth:\s*([0-9.]+) MB/sec"), "pipe_bandwidth", "MB/s", "higher"),
    (re.compile(r"^Signal handler installation:\s*([0-9.]+) microseconds"), "signal_install", "us", "lower"),
    (re.compile(r"^Signal handler overhead:\s*([0-9.]+) microseconds"), "signal_catch", "us", "lower"),
]


def run(args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def build(build_dir: Path) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    runner = build_dir / "bench_runner"
    result = run([
        CC, "-O2", "-static", f"-L{GNU_LIB}", "-o", str(runner),
        str(REPO / "tools/riscv/perf/bench_runner.c"),
    ])
    if result.returncode != 0:
        raise SystemExit(f"bench_runner build failed:\n{result.stderr.decode(errors='replace')[-800:]}")
    root = build_dir / "rootfs"
    if root.exists():
        shutil.rmtree(root)
    (root / "bin").mkdir(parents=True)
    (root / "bench").mkdir()
    (root / "tmp").mkdir()
    (root / "proc").mkdir()
    (root / "sys").mkdir()
    (root / "dev").mkdir()
    (root / "etc").mkdir()
    (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
    shutil.copy2(BUSYBOX, root / "bin/busybox")
    for applet in ("sh", "mount", "mkdir", "sleep", "cat", "ls"):
        (root / "bin" / applet).symlink_to("busybox")
    for binary in LMBENCH_BIN.iterdir():
        if binary.is_file():
            shutil.copy2(binary, root / "bench" / binary.name)
    shutil.copy2(runner, root / "init")
    os.chmod(root / "init", 0o755)
    initramfs = build_dir / "bench-initramfs.cpio.gz"
    subprocess.run(
        ["sh", "-c", f"cd {root} && find . | cpio -o -H newc 2>/dev/null | gzip -9 > {initramfs}"],
        check=True,
    )
    return initramfs


def boot(kernel: Path, initramfs: Path, append: str, log: Path) -> str:
    command = [
        "qemu-system-riscv64", "-machine", "virt", "-m", "2G", "-smp", "4",
        "-nographic", "-kernel", str(kernel), "-initrd", str(initramfs),
        "-append", append,
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
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
            if "BENCH_DONE" in text or "Uncaught panic" in text:
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
    log.write_text(text)
    return text


def parse(text: str) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for line in text.replace("\r", "").splitlines():
        line = line.strip()
        for pattern, case, unit, direction in PATTERNS:
            match = pattern.match(line)
            if match:
                values[case] = {"value": float(match.group(1)), "unit": unit, "direction": direction}
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asterinas", type=Path, default=REPO / "target/ltp/kernel-sv39.img")
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args(argv)

    output = args.output_dir.resolve()
    if output.exists():
        parser.error(f"output directory must be new: {output}")
    output.mkdir(parents=True)
    for kernel in (args.asterinas, args.linux):
        if not kernel.is_file():
            raise SystemExit(f"missing kernel: {kernel}")
    if not BUSYBOX.is_file() or not LMBENCH_BIN.is_dir():
        raise SystemExit("missing busybox or LMBench binaries")

    initramfs = build(output / "build")
    systems = {
        "asterinas": (args.asterinas, "console=ttyS0 loglevel=warn init=/init"),
        "linux": (args.linux, "console=ttyS0 earlycon=sbi"),
    }
    results: dict[str, dict] = {}
    for name, (kernel, append) in systems.items():
        samples: list[dict[str, dict[str, object]]] = []
        for repetition in range(args.repetitions):
            log = output / f"{name}-{repetition + 1}.log"
            text = boot(kernel, initramfs, append, log)
            samples.append(parse(text))
        # Median per case across repetitions.
        merged: dict[str, dict[str, object]] = {}
        for case in samples[0]:
            values = sorted(sample[case]["value"] for sample in samples if case in sample)
            if values:
                merged[case] = {
                    "value": values[len(values) // 2],
                    "min": values[0],
                    "max": values[-1],
                    **{k: samples[0][case][k] for k in ("unit", "direction")},
                }
        results[name] = merged

    report = {"systems": results, "ratios": {}}
    for case, entry in results.get("asterinas", {}).items():
        linux = results.get("linux", {}).get(case)
        if not linux:
            continue
        ratio = entry["value"] / linux["value"]
        report["ratios"][case] = {
            "asterinas": entry["value"],
            "linux": linux["value"],
            "unit": entry["unit"],
            "direction": entry["direction"],
            "asterinas_over_linux": ratio,
        }
    (output / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(f"{'case':20} {'asterinas':>10} {'linux':>10} {'ratio':>7}  unit")
    for case, row in report["ratios"].items():
        print(f"{case:20} {row['asterinas']:10.2f} {row['linux']:10.2f} "
              f"{row['asterinas_over_linux']:7.2f}  {row['unit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
