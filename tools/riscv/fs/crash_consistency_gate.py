#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Crash-consistency gate: kill QEMU mid-write, replay the journal, verify.

The guest mounts a virtio-blk image, appends deterministic 64-byte records and
fsyncs every eight. The host waits for a durable batch, kills QEMU at a random
point, runs e2fsck on a copy (which replays an ext4 journal) and reads the file
back with debugfs. Requirements:

* e2fsck leaves a consistent filesystem (exit 0 or 1);
* every record with sequence <= the last CRASH_SYNC marker is present and
  intact (fsync durability);
* records are consecutive from 1 and checksummed; a short or corrupt tail is
  allowed only after the last valid record.

Usage:
  tools/riscv/fs/crash_consistency_gate.py --output-dir /tmp/crash --fs both
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_KERNEL = REPO / "target/ltp/kernel-sv39.img"
BUSYBOX = REPO / "target/nixos/busybox"
CC = "riscv64-linux-musl-gcc"
GNU_LIB = "/usr/riscv64-linux-gnu/lib"
RECORD_BYTES = 64
MAGIC = 0x4153545245434431
IMAGE_BYTES = 128 * 1024 * 1024
GUEST_TIMEOUT = 90.0


class GateError(RuntimeError):
    pass


def run(args: list[str], *, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise GateError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-500:]}")
    return result


def build_writer(build: Path) -> Path:
    writer = build / "writer"
    if not writer.exists():
        run([
            CC, "-O2", "-static", f"-L{GNU_LIB}", "-o", str(writer),
            str(REPO / "tools/riscv/fs/crash_writer.c"),
        ])
    return writer


def build_initramfs(build: Path, writer: Path, mode: str = "") -> Path:
    root = build / "rootfs"
    if root.exists():
        shutil.rmtree(root)
    (root / "bin").mkdir(parents=True)
    (root / "proc").mkdir()
    (root / "sys").mkdir()
    (root / "mnt").mkdir()
    shutil.copy2(BUSYBOX, root / "bin/busybox")
    for applet in ("sh", "mount", "mkdir", "ls", "grep", "cat"):
        (root / "bin" / applet).symlink_to("busybox")
    shutil.copy2(writer, root / "writer")
    shutil.copy2(REPO / "tools/riscv/fs/init.sh", root / "init")
    os.chmod(root / "init", 0o755)
    if mode:
        (root / "mode").write_text(mode + "\n")
    initramfs = build / "crash-initramfs.cpio.gz"
    with (build / "initramfs.list").open("w") as listing:
        run(["sh", "-c", f"cd {root} && find . | cpio -o -H newc 2>/dev/null | gzip -9 > {initramfs}"])
    return initramfs


def prepare_image(case: Path, fs_type: str) -> Path:
    image = case / "disk.img"
    with image.open("wb") as output:
        output.truncate(IMAGE_BYTES)
    # The kernel's ext2/ext4 compatibility driver accepts exactly this
    # feature set: 4 KiB blocks, extents, 64-bit group descriptors, flex_bg,
    # the ext4 inode/file extensions, metadata checksums and a journal; every
    # ro-compat bit outside {sparse_super, large_file, btree_dir, huge_file,
    # dir_nlink, extra_isize, metadata_csum} and every unknown incompat bit is
    # rejected at mount time. ext4 keeps the checksums on (the default mke2fs
    # layout); ext2 images stay on the conservative set below.
    features = "^metadata_csum,^metadata_csum_seed,^quota,^bigalloc"
    features += ",^casefold,^encrypt,^inline_data"
    if fs_type == "ext2":
        # The ext4-only extensions would drag in extents/64-bit descriptors.
        features += ",^flex_bg,^huge_file,^dir_nlink,^extra_isize"
    else:
        # Exercise the metadata-checksum path on the crash gate too.
        features = features.replace("^metadata_csum,", "").replace(
            "^metadata_csum_seed,", ""
        )
    run(["mke2fs", "-q", "-F", "-t", fs_type, "-b", "4096", "-O", features, str(image)])
    fsck = run(["e2fsck", "-fn", str(image)], check=False)
    if fsck.returncode != 0:
        raise GateError(f"fresh {fs_type} image failed fsck: {fsck.returncode}")
    return image


def launch(
    case: Path, kernel: Path, initramfs: Path, image: Path, extra_args: str = ""
) -> subprocess.Popen[bytes]:
    command = [
        "qemu-system-riscv64", "-machine", "virt", "-m", "2G", "-smp", "2",
        "-nographic", "-kernel", str(kernel), "-initrd", str(initramfs),
        "-append", f"console=ttyS0 loglevel=warn init=/init {extra_args}".strip(),
        "-drive", f"if=none,format=raw,file={image},id=d0",
        "-device", "virtio-blk-device,drive=d0",
    ]
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def wait_and_cut(case: Path, process: subprocess.Popen[bytes], *, min_seconds: float, max_seconds: float) -> tuple[str, int]:
    """Wait for the first durable batch, then cut at a random point."""
    text = ""
    last_sync = 0
    ready_at = None
    deadline = time.monotonic() + GUEST_TIMEOUT
    while time.monotonic() < deadline:
        line = process.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if process.poll() is not None:
                break
            continue
        text += line.decode("utf-8", "replace")
        if "CRASH_SYNC " in text:
            try:
                last_sync = max(
                    int(part.split()[1]) for part in text.splitlines() if part.startswith("CRASH_SYNC ")
                )
            except (IndexError, ValueError):
                pass
        if "CRASH_SYNC " in text and ready_at is None:
            ready_at = time.monotonic()
        if ready_at is not None and time.monotonic() - ready_at >= random.uniform(min_seconds, max_seconds):
            kill_tree(process)
            break
    timed_out = False
    while time.monotonic() < deadline:
        line = process.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if process.poll() is not None:
                break
            continue
        text += line.decode("utf-8", "replace")
        if "CRASH_SYNC " in text:
            try:
                last_sync = max(
                    int(part.split()[1]) for part in text.splitlines() if part.startswith("CRASH_SYNC ")
                )
            except (IndexError, ValueError):
                pass
        if ready_at is None and "CRASH_SYNC " in text:
            ready_at = time.monotonic()
        if ready_at is not None and time.monotonic() - ready_at >= random.uniform(min_seconds, max_seconds):
            kill_tree(process)
            break
    else:
        timed_out = True
        kill_tree(process)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        process.wait(timeout=10)
    (case / "serial.log").write_text(text)
    if timed_out:
        raise GateError(f"guest did not reach a durable batch in time: {text[-400:]!r}")
    if "CRASH_READY" not in text:
        raise GateError(f"guest never mounted the filesystem or opened the data file: {text[-400:]!r}")
    if last_sync == 0:
        raise GateError(f"guest never reported a durable batch: {text[-400:]!r}")
    return text, last_sync


def parse_records(data: bytes) -> tuple[int, str | None]:
    """Returns (count of valid consecutive records, first defect or None)."""
    count = 0
    offset = 0
    while offset + RECORD_BYTES <= len(data):
        magic, seq = struct.unpack_from("<QQ", data, offset)
        if magic != MAGIC:
            return count, f"bad magic at record {count + 1}"
        if seq != count + 1:
            return count, f"sequence {seq} at position {count + 1}"
        expected = [
            (seq * 0x9E3779B97F4A7C15 + i * 0xC2B2AE3D27D4EB4F) & 0xFFFFFFFFFFFFFFFF
            for i in range(6)
        ]
        actual = struct.unpack_from("<6Q", data, offset + 16)
        if list(actual) != expected:
            return count, f"payload mismatch at record {seq}"
        count += 1
        offset += RECORD_BYTES
    if offset != len(data):
        return count, "short tail record"
    return count, None


def recover_and_verify(case: Path, image: Path, last_sync: int) -> dict[str, object]:
    replay = case / "disk.replayed.img"
    shutil.copy2(image, replay)
    fsck = run(["e2fsck", "-fy", str(replay)], check=False, timeout=120)
    (case / "fsck-after.log").write_text(fsck.stdout + fsck.stderr)
    if fsck.returncode not in (0, 1):
        raise GateError(f"e2fsck could not repair the image: exit {fsck.returncode}")
    dump = case / "data.bin"
    with dump.open("wb") as output:
        debugfs = subprocess.run(
            ["debugfs", "-R", "cat /data", str(replay)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    if debugfs.returncode != 0:
        raise GateError(f"debugfs could not read /data: {debugfs.stderr.decode(errors='replace')[-300:]}")
    data = debugfs.stdout
    dump.write_bytes(data)
    # Keep only the evidence: the 128 MiB replay copy is disposable and /tmp
    # is a small tmpfs on the development host.
    replay.unlink(missing_ok=True)
    valid, defect = parse_records(data)
    if defect is not None and not defect.startswith("short tail"):
        raise GateError(f"recovered data is corrupt: {defect}")
    if valid < last_sync:
        raise GateError(
            f"lost fsynced data: recovered {valid} records, last durable marker {last_sync}"
        )
    return {
        "fsck_exit": fsck.returncode,
        "records_valid": valid,
        "last_sync_seen": last_sync,
        "tail": "short" if defect else "clean",
        "bytes": len(data),
    }


def run_case(case: Path, kernel: Path, initramfs: Path, fs_type: str) -> dict[str, object]:
    case.mkdir(parents=True)
    image = prepare_image(case, fs_type)
    process = launch(case, kernel, initramfs, image)
    try:
        _, last_sync = wait_and_cut(case, process, min_seconds=0.3, max_seconds=2.5)
    finally:
        if process.poll() is None:
            kill_tree(process)
    return recover_and_verify(case, image, last_sync)


DANGLING_MARKERS = ("Block bitmap differences", "Inode bitmap differences")


def parse_bitmap_differences(log: str) -> tuple[list[int], list[int]]:
    """Returns (dangling, leaked) block/inode ids from `e2fsck -fn` output.

    A `+` entry is a block (or inode) an on-disk structure references while the
    bitmap calls it free: a dangling reference, and the corruption this gate
    exists to catch. A `-` entry is the opposite -- allocated but unreferenced,
    which a crash may legitimately leave behind and `e2fsck` reclaims.
    """
    dangling: list[int] = []
    leaked: list[int] = []
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith(DANGLING_MARKERS):
            continue
        tokens = line.split()
        cursor = index + 1
        while cursor < len(lines):
            extra = lines[cursor].strip().split()
            if not extra or not all(t[:1] in "+-" and t[1:].isdigit() for t in extra):
                break
            tokens.extend(extra)
            cursor += 1
        for token in tokens:
            if len(token) > 1 and token[0] in "+-" and token[1:].isdigit():
                (dangling if token[0] == "+" else leaked).append(int(token[1:]))
    return dangling, leaked


def wait_for_churn(case: Path, process: subprocess.Popen[bytes]) -> str:
    """Waits for the churn writer to start, then kills QEMU mid-churn."""
    text = ""
    ready_at = None
    deadline = time.monotonic() + GUEST_TIMEOUT
    while time.monotonic() < deadline:
        line = process.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if process.poll() is not None:
                break
            continue
        text += line.decode("utf-8", "replace")
        if "CHURN_READY" in text and ready_at is None:
            ready_at = time.monotonic()
        # Each round announces itself and then starts a held sync, so a kill
        # anywhere in this window lands between the inode table and the
        # bitmaps.
        if ready_at is not None and time.monotonic() - ready_at >= random.uniform(0.4, 1.2):
            kill_tree(process)
            break
    else:
        kill_tree(process)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        process.wait(timeout=10)
    (case / "serial.log").write_text(text)
    if "CHURN_READY" not in text:
        raise GateError(f"guest never started the churn writer: {text[-400:]!r}")
    if text.count("CHURN ") < 1:
        raise GateError(f"guest never announced a churn round: {text[-400:]!r}")
    return text


def verify_churn(case: Path, image: Path) -> dict[str, object]:
    check = case / "disk.check.img"
    shutil.copy2(image, check)
    before = run(["e2fsck", "-fn", str(check)], check=False, timeout=120)
    log = before.stdout + before.stderr
    (case / "fsck-before.log").write_text(log)
    dangling, leaked = parse_bitmap_differences(log)
    if dangling:
        raise GateError(
            "crash left dangling references (bitmap says free, something points at it): "
            f"{dangling[:16]}"
        )
    repair = run(["e2fsck", "-fy", str(check)], check=False, timeout=120)
    (case / "fsck-repair.log").write_text(repair.stdout + repair.stderr)
    if repair.returncode not in (0, 1):
        raise GateError(f"e2fsck could not repair the churned image: exit {repair.returncode}")
    after = run(["e2fsck", "-fn", str(check)], check=False, timeout=120)
    if after.returncode != 0:
        raise GateError(
            "image is still inconsistent after repair:\n"
            + (after.stdout + after.stderr)[-600:]
        )
    check.unlink(missing_ok=True)
    return {
        "fsck_before_exit": before.returncode,
        "dangling": 0,
        "leaked_reclaimed": len(leaked),
    }


def run_churn_case(case: Path, kernel: Path, initramfs: Path, fs_type: str) -> dict[str, object]:
    case.mkdir(parents=True)
    image = prepare_image(case, fs_type)
    process = launch(
        case,
        kernel,
        initramfs,
        image,
        # Open the window between the inode table and the bitmaps so a kill can
        # land in it deterministically.
        extra_args="asterinas.ext2_sync_hold_ms=1500",
    )
    try:
        wait_for_churn(case, process)
    finally:
        if process.poll() is None:
            kill_tree(process)
    return verify_churn(case, image)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL)
    parser.add_argument("--fs", choices=("ext2", "ext4", "both"), default="both")
    parser.add_argument(
        "--writer",
        choices=("records", "churn"),
        default="records",
        help="churn: create/write/truncate/unlink without fsync; checks that a "
        "crash cannot leave an inode pointing at a block the bitmap calls free",
    )
    parser.add_argument("--iterations", type=int, default=2)
    args = parser.parse_args(argv)

    output = args.output_dir.resolve()
    if output.exists():
        parser.error(f"output directory must be new: {output}")
    output.mkdir(parents=True)
    if not args.kernel.is_file():
        raise SystemExit(f"missing kernel image: {args.kernel}")
    if not BUSYBOX.is_file():
        raise SystemExit(f"missing static busybox: {BUSYBOX}")

    build = output / "build"
    build.mkdir()
    writer = build_writer(build)
    initramfs = build_initramfs(build, writer, "churn" if args.writer == "churn" else "")
    if args.writer == "churn" and args.fs == "both":
        # The churn check is about the no-journal ordering; ext4 replays its
        # journal here and would not exercise the same window.
        args.fs = "ext2"

    types = ("ext2", "ext4") if args.fs == "both" else (args.fs,)
    result: dict[str, object] = {"status": "running", "cases": []}
    failures = 0
    for fs_type in types:
        for iteration in range(args.iterations):
            name = f"{fs_type}-{iteration + 1}"
            case = output / name
            try:
                if args.writer == "churn":
                    evidence = run_churn_case(case, args.kernel, initramfs, fs_type)
                    summary = (
                        f"dangling={evidence['dangling']} "
                        f"reclaimed={evidence['leaked_reclaimed']}"
                    )
                else:
                    evidence = run_case(case, args.kernel, initramfs, fs_type)
                    summary = (
                        f"valid={evidence['records_valid']} "
                        f"sync={evidence['last_sync_seen']} tail={evidence['tail']}"
                    )
                evidence["case"] = name
                result["cases"].append(evidence)
                print(f"{name}: ok ({summary})", flush=True)
            except GateError as error:
                failures += 1
                print(f"{name}: FAILED ({error})", flush=True)
                result["cases"].append({"case": name, "error": str(error)})
    result["status"] = "passed" if failures == 0 else "failed"
    (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
