#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the Debian ext4 regression workload and retain verifiable artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
MARKER = "ASTERINAS_EXT4_REGRESSION_OK operations=basic,extent,syscalls"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    image = args.image.resolve()
    output = args.output_dir.resolve()
    if not image.is_file() or not output.is_relative_to(REPO) or output.exists():
        parser.error("image must exist and output-dir must be a new directory in the repository")
    output.mkdir(parents=True)
    run_image = output / "debian-root.ext2"
    shutil.copyfile(image, run_image)
    guest_output = "/root/asterinas/" + str(output.relative_to(REPO))
    guest_image = guest_output + "/debian-root.ext2"
    command = [
        str(REPO / "tools/docker/run_dev_container.sh"),
        "--workspace", str(REPO), "--", "env",
        f"ASTERINAS_EXT2_DRIVE_FILE={guest_image}",
        f"ASTERINAS_QEMU_LOG_DIR={guest_output}",
        "make", "run_kernel", "TARGET_ARCH=riscv64", "SMP=4",
        "FEATURES=riscv_sv39_mode", "AUTO_TEST=ext4_regression", "RELEASE=1",
    ]
    (output / "command.txt").write_text(" ".join(command) + "\n")
    status = 1
    try:
        with (output / "runner.log").open("w") as log:
            completed = subprocess.run(
                command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                timeout=args.timeout, check=False,
            )
        status = completed.returncode
    except subprocess.TimeoutExpired:
        (output / "timeout.txt").write_text(f"timeout_seconds={args.timeout}\n")
    qemu_log = output / "qemu.log"
    fsck_log = output / "e2fsck.log"
    fsck = subprocess.run(["e2fsck", "-fn", str(run_image)], capture_output=True, text=True)
    fsck_log.write_text(fsck.stdout + fsck.stderr)
    marker = qemu_log.is_file() and MARKER in qemu_log.read_text(errors="replace")
    result = {"qemu_exit": status, "fsck_exit": fsck.returncode, "marker": marker}
    (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    files = sorted(path for path in output.iterdir() if path.name != "SHA256SUMS")
    with (output / "SHA256SUMS").open("w") as sums:
        for path in files:
            if path.is_file():
                sums.write(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    return 0 if status == 0 and fsck.returncode == 0 and marker else 1


if __name__ == "__main__":
    sys.exit(main())
