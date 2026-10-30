#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Put the board's kernel image inside the root filesystem image.

U-Boot's stored boot sequence loads the kernel from the writable root
partition (`ext4load mmc 1:2 /root/kernel-rel.img`), so a freshly built rootfs
without that file boots nothing. Doing this by hand with `debugfs -w` works but
is easy to get wrong -- an earlier attempt left the image without the kernel and
the board would not have started.

    prepare_board_image.py --image target/debian-riscv/browser-web/rootfs/debian-root.ext2 \
        --kernel target/osdk/aster-kernel-osdk-bin.Image

Needs root for `debugfs -w`; the image is modified in place and its new hash is
printed.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

DEFAULT_DEST = "/root/kernel-rel.img"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def debugfs(
    image: Path, command: str, *, write: bool, allow_failure: bool = False
) -> str:
    argv = ["debugfs"]
    if write:
        argv.append("-w")
    argv += ["-R", command, str(image)]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0 and not allow_failure:
        raise SystemExit(
            f"debugfs {command!r} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def inject(image: Path, kernel: Path, dest: str) -> str:
    if not image.is_file():
        raise SystemExit(f"no such image: {image}")
    if not kernel.is_file():
        raise SystemExit(f"no such kernel: {kernel}")
    expected = sha256_of(kernel)

    # A freshly made filesystem has no /root; creating it is cheap and makes
    # the script work on an image that was not built by our rootfs builder.
    parent = str(Path(dest).parent)
    if parent not in ("", "/"):
        debugfs(image, f"mkdir {parent}", write=True, allow_failure=True)
    # A leftover file from a previous run would be replaced, but removing it
    # first keeps the inode count from growing with each preparation.
    debugfs(image, f"rm {dest}", write=True, allow_failure=True)
    debugfs(image, f"write {kernel} {dest}", write=True)

    read_back = subprocess.run(
        ["debugfs", "-R", f"cat {dest}", str(image)],
        capture_output=True,
        check=False,
    )
    actual = hashlib.sha256(read_back.stdout).hexdigest()
    if read_back.returncode != 0 or actual != expected:
        raise SystemExit(
            f"the kernel read back from the image does not match: {actual} != {expected}"
        )
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(prog="prepare_board_image")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--dest", default=DEFAULT_DEST)
    values = parser.parse_args()

    kernel_hash = inject(values.image, values.kernel, values.dest)
    print(f"kernel {values.kernel.name} -> {values.dest} sha256={kernel_hash}")
    print(f"image {values.image} sha256={sha256_of(values.image)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
