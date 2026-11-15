#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""fio comparison gate: tmpfs on Asterinas and Linux, ext4 on Asterinas.

Both systems boot the same initramfs (busybox + static fio + fio_runner). The
runner mounts tmpfs at /tmp, optionally mounts the ext4 virtio-blk image at
/mnt, and runs fio JSON jobs: sequential and random read/write at 4 KiB for
5 seconds each. The Linux baseline has no virtio-blk module in this initramfs,
so its ext4 cases are skipped; the comparison table is tmpfs, and Asterinas'
ext4 numbers are reported separately.

Usage:
  tools/riscv/perf/fio_gate.py --asterinas target/ltp/kernel-sv39.img \
      --linux target/perf/linux/root/usr/lib/modules/*/vmlinuz \
      --output-dir target/perf/fio-run-1
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
FIO = REPO / "target/perf/fio-bin/fio"
CC = "riscv64-linux-musl-gcc"
GNU_LIB = "/usr/riscv64-linux-gnu/lib"
IMAGE_BYTES = 256 * 1024 * 1024
TIMEOUT_SECONDS = 900
MOUNT_DONE = "FIO_BEGIN"

DECODER = json.JSONDecoder()


def run(args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def build(build_dir: Path) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    runner = build_dir / "fio_runner"
    result = run([
        CC, "-O2", "-static", f"-L{GNU_LIB}", "-o", str(runner),
        str(REPO / "tools/riscv/perf/fio_runner.c"),
    ])
    if result.returncode != 0:
        raise SystemExit(f"fio_runner build failed:\n{result.stderr.decode(errors='replace')[-600:]}")
    root = build_dir / "rootfs"
    if root.exists():
        shutil.rmtree(root)
    (root / "bin").mkdir(parents=True)
    (root / "dev").mkdir()
    (root / "proc").mkdir()
    (root / "tmp").mkdir()
    (root / "mnt").mkdir()
    shutil.copy2(BUSYBOX, root / "bin/busybox")
    for applet in ("sh", "mount", "mkdir", "sleep", "ls", "cat"):
        (root / "bin" / applet).symlink_to("busybox")
    shutil.copy2(FIO, root / "fio")
    shutil.copy2(runner, root / "init")
    os.chmod(root / "init", 0o755)
    initramfs = build_dir / "fio-initramfs.cpio.gz"
    subprocess.run(
        ["sh", "-c", f"cd {root} && find . | cpio -o -H newc 2>/dev/null | gzip -9 > {initramfs}"],
        check=True,
    )
    return initramfs


def prepare_disk(path: Path) -> None:
    with path.open("wb") as output:
        output.truncate(IMAGE_BYTES)
    features = "^metadata_csum,^orphan_file,^metadata_csum_seed,^quota,^bigalloc"
    features += ",^casefold,^encrypt,^inline_data,^flex_bg,^huge_file,^dir_nlink,^extra_isize"
    run(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-O", features, str(path)])


def boot(kernel: Path, initramfs: Path, append: str, disk: Path, log: Path) -> str:
    command = [
        "qemu-system-riscv64", "-machine", "virt", "-m", "2G", "-smp", "4",
        "-nographic", "-kernel", str(kernel), "-initrd", str(initramfs),
        "-append", append,
        "-drive", f"if=none,format=raw,file={disk},id=d0",
        "-device", "virtio-blk-device,drive=d0",
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
            if "FIO_DONE" in text or "Uncaught panic" in text:
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
    results: dict[str, dict[str, object]] = {}
    current = None
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            break
        try:
            document, end = DECODER.raw_decode(text[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        position = start + end
        if not isinstance(document, dict) or "jobs" not in document:
            continue
        for job in document.get("jobs", []):
            name = job.get("jobname")
            section = job.get("read", {}) if job.get("read", {}).get("io_bytes") else job.get("write", {})
            if not name or not section:
                continue
            results[name] = {
                "iops": section.get("iops"),
                "bw_kib": section.get("bw"),
                "latency_us": section.get("clat_ns", {}).get("mean", 0) / 1000.0,
            }
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asterinas", type=Path, default=REPO / "target/ltp/kernel-sv39.img")
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    output = args.output_dir.resolve()
    if output.exists():
        parser.error(f"output directory must be new: {output}")
    output.mkdir(parents=True)
    for kernel in (args.asterinas, args.linux):
        if not kernel.is_file():
            raise SystemExit(f"missing kernel: {kernel}")
    if not BUSYBOX.is_file() or not FIO.is_file():
        raise SystemExit("missing busybox or fio")

    initramfs = build(output / "build")
    disk = output / "disk.img"
    prepare_disk(disk)

    systems = {
        "asterinas": (args.asterinas, "console=ttyS0 loglevel=warn init=/init"),
        "linux": (args.linux, "console=ttyS0 earlycon=sbi"),
    }
    results: dict[str, dict] = {}
    for name, (kernel, append) in systems.items():
        text = boot(kernel, initramfs, append, disk, output / f"{name}.log")
        results[name] = parse(text)

    report = {"systems": results, "ratios": {}}
    for case, entry in results.get("asterinas", {}).items():
        if not case.startswith("tmpfs-"):
            continue
        linux = results.get("linux", {}).get(case)
        if not linux:
            continue
        report["ratios"][case] = {
            "asterinas_iops": entry["iops"],
            "linux_iops": linux["iops"],
            "iops_ratio": (entry["iops"] / linux["iops"]) if linux["iops"] else None,
            "asterinas_latency_us": entry["latency_us"],
            "linux_latency_us": linux["latency_us"],
        }
    (output / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(f"{'case':20} {'ast iops':>10} {'lin iops':>10} {'ratio':>7} {'ast lat us':>11} {'lin lat us':>11}")
    for case, row in report["ratios"].items():
        ratio = row["iops_ratio"]
        print(f"{case:20} {row['asterinas_iops']:10.0f} {row['linux_iops']:10.0f} "
              f"{(ratio if ratio is not None else 0):7.2f} "
              f"{row['asterinas_latency_us']:11.1f} {row['linux_latency_us']:11.1f}")
    print("\nAsterinas ext4 (virtio-blk, absolute):")
    for case, entry in results.get("asterinas", {}).items():
        if case.startswith("ext4-"):
            print(f"  {case:18} iops={entry['iops']:.0f} bw={entry['bw_kib'] / 1024:.1f} MB/s "
                  f"lat={entry['latency_us']:.1f} us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
