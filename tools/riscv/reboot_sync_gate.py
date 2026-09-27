#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Check that a raw reboot(2) persists ext2 changes across a QEMU restart.

Run in the persistent development container after building the RISC-V kernel.
The gate only writes a newly created output directory and its scratch disk.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SOURCE = Path(__file__).with_name("reboot_sync_init.c")
PAYLOAD = "asterinas-reboot-sync-v1\n"


def run(*argv: str, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, check=True, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kernel",
        type=Path,
        default=REPO_ROOT / "target/osdk/aster-kernel/aster-kernel-osdk-bin.Image",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    kernel = args.kernel.resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "root"
    (root / "ext2").mkdir(parents=True)
    init = root / "init"
    run(
        "riscv64-linux-gnu-gcc",
        "-nostdlib",
        "-static",
        "-fno-builtin",
        "-fno-pie",
        "-no-pie",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wl,-e,_start",
        str(INIT_SOURCE),
        "-o",
        str(init),
    )

    initramfs = output / "initramfs.cpio"
    names = b"\0".join(
        str(path.relative_to(root)).encode() for path in sorted(root.rglob("*"))
    ) + b"\0"
    with initramfs.open("wb") as stream:
        run("cpio", "--null", "-o", "--format=newc", input=names, cwd=root, stdout=stream)

    disk = output / "ext2.img"
    run(
        "mke2fs",
        "-q",
        "-F",
        "-t",
        "ext2",
        "-b",
        "4096",
        "-O",
        "^ext_attr,^resize_inode,^dir_index",
        str(disk),
        "32768",
    )

    command = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=true,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "4",
        "-nographic",
        "-monitor", "none",
        "-kernel", str(kernel),
        "-initrd", str(initramfs),
        "-append", "console=ttyS0 loglevel=4 init=/init",
        "-drive", f"if=none,format=raw,id=x0,file={disk}",
        "-device", "virtio-blk-device,drive=x0",
    ]
    serial = output / "qemu-serial.log"
    try:
        with serial.open("wb") as stream:
            guest = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=90)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"QEMU did not shut down within 90 s; see {serial}") from error

    log = serial.read_text(errors="replace")
    markers = {
        "boots": log.count("Enter riscv_boot"),
        "first_writes": log.count("REBOOT_SYNC_FIRST_WRITTEN boot=1"),
        "second_reads": log.count("REBOOT_SYNC_PASS boot=2"),
        "failures": log.count("REBOOT_SYNC_FAIL"),
    }
    with (output / "e2fsck.log").open("wb") as stream:
        fsck = subprocess.run(
            ["e2fsck", "-fn", str(disk)], stdout=stream, stderr=subprocess.STDOUT
        )
    contents = subprocess.run(
        ["debugfs", "-R", "cat /reboot-sync-payload", str(disk)],
        capture_output=True,
        text=True,
    )
    result = {
        "kernel_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
        "qemu_exit": guest.returncode,
        "e2fsck_exit": fsck.returncode,
        "markers": markers,
        "payload_matches": contents.stdout == PAYLOAD,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    if (
        guest.returncode != 0
        or fsck.returncode != 0
        or markers != {"boots": 2, "first_writes": 1, "second_reads": 1, "failures": 0}
        or contents.stdout != PAYLOAD
    ):
        raise RuntimeError(f"reboot sync gate failed: {result}; see {output}")
    print(f"reboot sync gate passed: {output}")


if __name__ == "__main__":
    main()
