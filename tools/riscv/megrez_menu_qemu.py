# SPDX-License-Identifier: MPL-2.0

"""Exercise standalone menu userspace with no disk or network attached."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import tty

from tools.riscv.debian.rootfs.gate_runtime import SerialConsole
from tools.riscv.megrez_probe import qemu_probe_argv
from tools.riscv.megrez_boot_menu import MODE_ARGS


def run(kernel: Path, initramfs: Path, mode: str, output: Path) -> dict:
    if mode not in ("basic", "probe-auto"):
        raise ValueError("unknown standalone mode")
    kernel_sha = hashlib.sha256(kernel.read_bytes()).hexdigest()
    initramfs_sha = hashlib.sha256(initramfs.read_bytes()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    argv = qemu_probe_argv(
        kernel,
        initramfs,
        MODE_ARGS["probe" if mode == "probe-auto" else "basic"],
    )
    started = time.monotonic()
    master, slave = os.openpty()
    tty.setraw(slave)
    serial = SerialConsole(master, max_bytes=4 * 1024 * 1024)
    try:
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    result = {
        "mode": mode,
        "argv": argv,
        "passed": False,
        "kernel_sha256": kernel_sha,
        "initramfs_sha256": initramfs_sha,
    }
    try:
        deadline = started + 120
        if mode == "basic":
            serial.wait_for(b"asterinas-basic# ", deadline)
            nonce = secrets.token_hex(16)
            cursor = serial.checkpoint()
            # Split the marker in the command so terminal echo is not evidence.
            serial.send(
                f'cat /proc/mounts; uname -m; echo BASIC_OK:{nonce[:16]}""{nonce[16:]}\n'.encode(),
                deadline,
            )
            output_bytes = serial.wait_for(
                f"BASIC_OK:{nonce}".encode(), deadline, start=cursor
            )
            if b"/proc proc" not in output_bytes or b"/sys sysfs" not in output_bytes:
                raise RuntimeError("Basic API filesystems are missing")
            serial.send(b"sync; reboot -f\n", deadline)
        else:
            serial.wait_for(b"count=1 status=pass", deadline)
            serial.wait_for(b"ASTERINAS_PROBE_AUTO_REBOOT", deadline)
        process.wait(timeout=15)
        if process.returncode != 0:
            raise RuntimeError(f"QEMU exited with {process.returncode}")
        result["passed"] = True
    except Exception as error:
        result["error"] = str(error)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        result["elapsed_seconds"] = time.monotonic() - started
        (output / "serial.log").write_bytes(serial.transcript)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        os.close(master)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument("--mode", choices=("basic", "probe-auto"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.kernel, args.initramfs, args.mode, args.output)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
