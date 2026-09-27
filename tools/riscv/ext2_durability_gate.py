#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run bounded QEMU ext2 crash-cut and flush-error checks on fresh images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


REPO = Path(__file__).resolve().parents[2]
PAYLOAD = "asterinas-durability-cut-v1\n"
CUT_MARKER = "ASTERINAS_EXT2_CUT_READY stage=directory_fsync"
EIO_MARKER = "ASTERINAS_EXT2_FLUSH_EIO_OK errno=5"
MSYNC_EIO_MARKER = "ASTERINAS_EXT2_MSYNC_EIO_OK errno=5"
GUEST_TIMEOUT_SECONDS = 240


class GateError(RuntimeError):
    """A guest result or its evidence failed validation."""


def command(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def prepare_image(case: Path) -> None:
    case.mkdir()
    image = case / "ext2.img"
    with image.open("wb") as output:
        output.truncate(128 * 1024 * 1024)
    command(
        [
            "mke2fs", "-q", "-F", "-t", "ext2", "-O",
            "^ext_attr,^resize_inode,^dir_index", str(image),
        ]
    )
    for name in ("exfat.img", "ltp_dev.img", "nvme0n1.img"):
        source = REPO / "test/initramfs/build" / name
        if not source.is_file():
            raise GateError(f"missing shared QEMU image: {source}")
        (case / name).symlink_to(os.path.relpath(source, case))
    fsck = subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True, text=True)
    (case / "fsck-before.log").write_text(fsck.stdout + fsck.stderr)
    if fsck.returncode != 0:
        raise GateError(f"fresh ext2 image failed fsck: {fsck.returncode}")


def guest_path(path: Path) -> str:
    return "/root/asterinas/" + str(path.relative_to(REPO))


def docker_container(run_log: Path) -> str:
    match = re.search(r"(?m)^Using ([a-zA-Z0-9_.-]+) \(", run_log.read_text(errors="replace"))
    if match is None:
        raise GateError("persistent Docker container name is missing from run log")
    return match.group(1)


def kill_guest(case: Path) -> None:
    container = docker_container(case / "run.log")
    processes = command(["docker", "exec", container, "ps", "-eww", "-o", "pid=,args="])
    image = guest_path(case / "ext2.img")
    config = guest_path(case / "blkdebug.conf")
    expected_drives = (
        f"id=x0,file={image}",
        f"id=x0,file=blkdebug:{config}:{image}",
    )
    matches = []
    for line in processes.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if (
            len(parts) == 2
            and parts[1].startswith("qemu-system-riscv64 ")
            and any(drive in parts[1] for drive in expected_drives)
        ):
            matches.append(parts[0])
    if len(matches) != 1:
        raise GateError(f"expected one matching QEMU process, found {len(matches)}")
    command(["docker", "exec", container, "kill", "-KILL", matches[0]])


def stop_guest(case: Path, process: subprocess.Popen[str]) -> None:
    """Avoid leaving a case's QEMU running if a marker or wait times out."""

    if process.poll() is None:
        try:
            kill_guest(case)
        except (GateError, OSError, subprocess.SubprocessError):
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def launch_guest(case: Path, auto_test: str, *, blkdebug: bool) -> subprocess.Popen[str]:
    env = [
        f"ASTERINAS_TEST_BUILD_DIR={guest_path(case)}",
        f"ASTERINAS_QEMU_LOG_DIR={guest_path(case)}",
    ]
    if blkdebug:
        config = case / "blkdebug.conf"
        config.write_text(
            '[inject-error]\nevent = "flush_to_disk"\nerrno = "5"\nonce = "on"\n'
        )
        drive = f"blkdebug:{guest_path(config)}:{guest_path(case / 'ext2.img')}"
        env.append(f"ASTERINAS_EXT2_DRIVE_FILE={drive}")
    args = [
        str(REPO / "tools/docker/run_dev_container.sh"),
        "--workspace",
        str(REPO),
        "--",
        "env",
        *env,
        "make",
        "run_kernel",
        "TARGET_ARCH=riscv64",
        "SMP=4",
        "FEATURES=riscv_sv39_mode",
        f"AUTO_TEST={auto_test}",
    ]
    log = (case / "run.log").open("w")
    try:
        process = subprocess.Popen(args, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, text=True)
    finally:
        log.close()
    return process


def wait_for_marker(process: subprocess.Popen[str], log: Path, marker: str) -> None:
    deadline = time.monotonic() + GUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if log.exists() and marker in log.read_text(errors="replace"):
            return
        if process.poll() is not None:
            raise GateError(f"guest exited before {marker!r}: status={process.returncode}")
        time.sleep(0.25)
    raise GateError(f"guest did not reach {marker!r} within {GUEST_TIMEOUT_SECONDS}s")


def run_directory_cut(case: Path) -> dict[str, object]:
    prepare_image(case)
    process = launch_guest(case, "ext2_directory_fsync_cut", blkdebug=False)
    try:
        wait_for_marker(process, case / "qemu.log", CUT_MARKER)
        kill_guest(case)
        guest_exit = process.wait(timeout=30)
    finally:
        stop_guest(case, process)
    if guest_exit == 0:
        raise GateError("QEMU exited cleanly instead of being cut after directory fsync")
    fsck = subprocess.run(
        ["e2fsck", "-fn", str(case / "ext2.img")], capture_output=True, text=True
    )
    (case / "fsck-after.log").write_text(fsck.stdout + fsck.stderr)
    data = command(["debugfs", "-R", "cat /asterinas_durability_cut", str(case / "ext2.img")])
    if fsck.returncode != 0 or data.stdout != PAYLOAD:
        raise GateError(
            f"directory fsync cut failed: fsck={fsck.returncode}, payload={data.stdout!r}"
        )
    return {
        "marker": CUT_MARKER,
        "qemu_exit_after_sigkill": guest_exit,
        "fsck_exit": 0,
        "payload": PAYLOAD,
    }


def run_flush_eio(case: Path) -> dict[str, object]:
    prepare_image(case)
    process = launch_guest(case, "ext2_flush_eio", blkdebug=True)
    try:
        guest_exit = process.wait(timeout=GUEST_TIMEOUT_SECONDS)
    finally:
        stop_guest(case, process)
    log = (case / "qemu.log").read_text(errors="replace")
    if guest_exit != 0 or EIO_MARKER not in log:
        raise GateError(
            f"flush EIO test failed: qemu exit={guest_exit}, marker={EIO_MARKER in log}"
        )
    return {"marker": EIO_MARKER, "qemu_exit": 0}


def run_msync_eio(case: Path) -> dict[str, object]:
    prepare_image(case)
    process = launch_guest(case, "ext2_msync_eio", blkdebug=True)
    try:
        guest_exit = process.wait(timeout=GUEST_TIMEOUT_SECONDS)
    finally:
        stop_guest(case, process)
    log = (case / "qemu.log").read_text(errors="replace")
    if guest_exit != 0 or MSYNC_EIO_MARKER not in log:
        raise GateError(
            f"msync EIO test failed: qemu exit={guest_exit}, "
            f"marker={MSYNC_EIO_MARKER in log}"
        )
    return {"marker": MSYNC_EIO_MARKER, "qemu_exit": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--case", choices=("directory-fsync-cut", "flush-eio", "msync-eio")
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if not output.is_relative_to(REPO) or output.exists():
        parser.error("output directory must be new and inside this repository")
    output.mkdir(parents=True)
    result: dict[str, object] = {"status": "running", "cases": {}}
    cases = (
        ("directory-fsync-cut", run_directory_cut),
        ("flush-eio", run_flush_eio),
        ("msync-eio", run_msync_eio),
    )
    if args.case is not None:
        cases = tuple(case for case in cases if case[0] == args.case)
    try:
        for name, runner in cases:
            result["cases"][name] = runner(output / name)
            print(f"{name}: passed", flush=True)
        result["status"] = "passed"
    except BaseException as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        result["qemu_log_sha256"] = {
            name: hashlib.sha256(log.read_bytes()).hexdigest()
            for name, _ in cases
            if (log := output / name / "qemu.log").exists()
        }
        (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
