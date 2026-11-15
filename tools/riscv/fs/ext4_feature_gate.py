#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""ext4 feature-surface gate.

Boots the kernel with a virtio-blk image and runs `ext4_probe` as init. Three
images are exercised:

* `baseline` - the conservative feature set the driver has always accepted;
  the probe must complete with zero failures (no regression).
* `modern-ext4` - `mke2fs -t ext4` with metadata checksums turned off, i.e.
  flex_bg, huge_file, dir_nlink, extra_isize and orphan_file on; the probe
  must complete, and the image must survive `e2fsck -fn` with the probe's
  files intact.
* `default-ext4` - stock `mke2fs -t ext4`, which enables metadata_csum; the
  probe must complete and the image must survive `e2fsck -fn`, i.e. every
  metadata structure the driver touched carries a valid CRC32C.

Usage:
  tools/riscv/fs/ext4_feature_gate.py --kernel target/osdk/aster-kernel-osdk-bin.Image \
      --output-dir target/fs/ext4-features/run-1
"""

from __future__ import annotations

import argparse
import errno as errno_mod
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CC = "riscv64-linux-musl-gcc"
GNU_LIB = "/usr/riscv64-linux-gnu/lib"
IMAGE_BYTES = 256 * 1024 * 1024
GUEST_TIMEOUT = 420.0

CONSERVATIVE = (
    "^metadata_csum,^orphan_file,^metadata_csum_seed,^quota,^bigalloc"
    ",^casefold,^encrypt,^inline_data,^flex_bg,^huge_file,^dir_nlink,^extra_isize"
)

CASES = (
    {
        "name": "baseline",
        "features": CONSERVATIVE,
        "expect": "pass",
    },
    {
        "name": "modern-ext4",
        "features": "^metadata_csum,^metadata_csum_seed,^quota,^bigalloc,^casefold,^encrypt,^inline_data",
        "expect": "pass",
    },
    {
        "name": "default-ext4",
        "features": None,
        "expect": "pass",
    },
)


class GateError(RuntimeError):
    pass


def run(args: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def build_initramfs(build: Path) -> Path:
    build.mkdir(parents=True, exist_ok=True)
    probe = build / "ext4_probe"
    result = run([
        CC, "-O2", "-static", f"-L{GNU_LIB}", "-o", str(probe),
        str(REPO / "tools/riscv/fs/ext4_probe.c"),
    ])
    if result.returncode != 0:
        raise GateError(f"probe build failed:\n{result.stderr[-800:]}")
    root = build / "rootfs"
    if root.exists():
        shutil.rmtree(root)
    (root / "mnt").mkdir(parents=True)
    shutil.copy2(probe, root / "init")
    os.chmod(root / "init", 0o755)
    initramfs = build / "ext4-probe-initramfs.cpio.gz"
    subprocess.run(
        ["sh", "-c", f"cd {root} && find . | cpio -o -H newc 2>/dev/null | gzip -9 > {initramfs}"],
        check=True,
    )
    return initramfs


def prepare_image(case_dir: Path, features: str | None) -> Path:
    image = case_dir / "disk.img"
    with image.open("wb") as output:
        output.truncate(IMAGE_BYTES)
    command = ["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096"]
    if features is not None:
        command += ["-O", features]
    command.append(str(image))
    result = run(command)
    if result.returncode != 0:
        raise GateError(f"mke2fs failed: {result.stderr[-400:]}")
    listing = run(["dumpe2fs", "-h", str(image)])
    features_line = next(
        (line for line in listing.stdout.splitlines() if line.startswith("Filesystem features")),
        "Filesystem features: ",
    )
    (case_dir / "features.txt").write_text(features_line + "\n")
    return image


def boot(kernel: Path, initramfs: Path, image: Path, log: Path) -> str:
    command = [
        "qemu-system-riscv64", "-machine", "virt", "-m", "2G", "-smp", "2",
        "-nographic", "-kernel", str(kernel), "-initrd", str(initramfs),
        "-append", "console=ttyS0 loglevel=warn init=/init",
        "-drive", f"if=none,format=raw,file={image},id=d0",
        "-device", "virtio-blk-device,drive=d0",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    text = ""
    deadline = time.monotonic() + GUEST_TIMEOUT
    try:
        while time.monotonic() < deadline:
            # A bare readline() blocks forever when the guest goes silent, so
            # wait for readability with a timeout and keep the deadline alive.
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()  # type: ignore[union-attr]
            if not line:
                if process.poll() is not None:
                    break
                continue
            chunk = line.decode("utf-8", "replace")
            text += chunk
            if "EXT4_PROBE_DONE" in text or "Uncaught panic" in text:
                break
    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    log.write_text(text)
    return text


def host_verify(image: Path) -> list[str]:
    problems: list[str] = []
    fsck = run(["e2fsck", "-fn", str(image)])
    if fsck.returncode != 0:
        problems.append(f"e2fsck -fn exited {fsck.returncode}: {fsck.stdout[-400:]}")

    def debugfs_stat(name: str) -> str:
        result = run(["debugfs", "-R", f"stat /{name}", str(image)])
        return result.stdout + result.stderr

    probe = debugfs_stat("probe.dat")
    if "Size: 262144" not in probe:
        problems.append(f"probe.dat size mismatch: {probe.splitlines()[:2]}")
    big = debugfs_stat("big.bin")
    if "Size: 8388608" not in big:
        problems.append(f"big.bin size mismatch: {big.splitlines()[:2]}")
    present = debugfs_stat("f001")
    gone = debugfs_stat("f000")
    if "Inode:" not in present:
        problems.append("f001 is missing")
    if "File not found" not in gone and "Inode:" in gone:
        problems.append("f000 should have been unlinked")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, default=REPO / "target/osdk/aster-kernel-osdk-bin.Image")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    output = args.output_dir.resolve()
    if output.exists():
        parser.error(f"output directory must be new: {output}")
    output.mkdir(parents=True)
    if not args.kernel.is_file():
        raise SystemExit(f"missing kernel: {args.kernel}")

    initramfs = build_initramfs(output / "build")
    results = []
    failed = False
    for case in CASES:
        case_dir = output / case["name"]
        case_dir.mkdir()
        image = prepare_image(case_dir, case["features"])
        text = boot(args.kernel, initramfs, image, case_dir / "serial.log")

        outcome = "unknown"
        problems: list[str] = []
        if "EXT4_PROBE_FAIL mount" in text:
            # The probe still prints EXT4_PROBE_DONE after a mount failure, so
            # the rejection must be recognized before the completion marker.
            errno_match = re.search(r"EXT4_PROBE_FAIL mount errno=(\d+)", text)
            code = int(errno_match.group(1)) if errno_match else -1
            if case["expect"] == "mount-rejected" and code in (errno_mod.EINVAL, errno_mod.EOPNOTSUPP):
                outcome = "mount-rejected"
            else:
                outcome = "mount-failed"
                problems = [f"mount errno={code}"]
        elif "EXT4_PROBE_DONE" in text:
            match = re.search(r"EXT4_PROBE_DONE failures=(\d+)", text)
            failures = int(match.group(1)) if match else -1
            if failures == 0:
                outcome = "pass"
            else:
                outcome = "probe-failed"
                problems = [line for line in text.splitlines() if "EXT4_PROBE_FAIL" in line]
        elif "Uncaught panic" in text:
            outcome = "panic"
            problems = ["kernel panic"]
        else:
            outcome = "no-marker"
            problems = [text.splitlines()[-1] if text.splitlines() else "empty log"]

        expected = case["expect"]
        ok = outcome == expected or (expected == "pass" and outcome == "pass")
        if ok and outcome == "pass":
            problems += host_verify(image)
            ok = not problems
        if not ok:
            failed = True
        results.append({"case": case["name"], "outcome": outcome, "expected": expected, "ok": ok, "problems": problems})
        print(f"[{'PASS' if ok else 'FAIL'}] {case['name']}: {outcome} (expected {expected})")
        for problem in problems:
            print(f"    {problem}")

    (output / "result.json").write_text(json.dumps({"cases": results}, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
