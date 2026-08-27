#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boots the M22 resource-lifetime stress gate under virgl-enabled QEMU."""

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path


WORKDIR = Path(__file__).resolve().parents[4] / "target" / "drm-m22"
EVIDENCE = WORKDIR / "evidence"
BOOT_DISK = WORKDIR / "boot.ext4"
U_BOOT = WORKDIR / "u-boot"
BOOTARGS = "console=ttyS0 loglevel=info init=/init"
SMP = os.environ.get("M22_SMP", "4")
KERNEL_TIMEOUT = int(os.environ.get("M22_KERNEL_TIMEOUT", "300"))

COMMANDS = [
    (b"virtio scan", b"=>", 30),
    (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 30),
    (b"ext4load virtio 0:0 0x88000000 /qemu-virt.dtb", b"bytes read", 10),
    (b"fdt addr 0x88000000", b"Working FDT set", 5),
    (b"fdt resize 0x1000", b"=>", 5),
    (b'setenv bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b'fdt set /chosen bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 30),
    (b"setenv initrd_size ${filesize}", b"=>", 5),
]
BOOT_COMMAND = b"booti 0x80200000 0x83000000:${initrd_size} 0x88000000"


def run() -> bytes:
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "1G",
        "-smp", SMP,
        "-display", "egl-headless,gl=on",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(U_BOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-gl-device",
    ]
    master_fd, slave_fd = pty.openpty()
    process = None
    output = b""

    def read_until(pattern: bytes, start: int, timeout: int) -> int | None:
        nonlocal output
        deadline = time.time() + timeout
        while time.time() < deadline:
            match = output.find(pattern, start)
            if match >= 0:
                return match + len(pattern)
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if not readable:
                continue
            try:
                data = os.read(master_fd, 65536)
            except OSError:
                return None
            if not data:
                return None
            output += data
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        return None

    def send(command: bytes) -> None:
        os.write(master_fd, command + b"\r\n")

    try:
        process = subprocess.Popen(
            argv, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True
        )
        os.close(slave_fd)
        slave_fd = -1

        prompt_end = read_until(b"=>", 0, 60)
        if prompt_end is None:
            print("[boot] U-Boot timeout")
        else:
            boot_ready = True
            for command, expected, timeout in COMMANDS:
                command_start = len(output)
                send(command)
                time.sleep(0.3)
                prompt_end = read_until(b"=>", command_start, timeout)
                if prompt_end is None or expected not in output[command_start:prompt_end]:
                    print(f"[boot] missed expected output for {command.decode()}")
                    boot_ready = False
                    break

            if boot_ready:
                command_start = len(output)
                send(BOOT_COMMAND)
                if read_until(b"Starting kernel", command_start, 30) is None:
                    print("[boot] kernel start timeout")
                    boot_ready = False

            if boot_ready:
                deadline = time.time() + KERNEL_TIMEOUT
                while time.time() < deadline:
                    readable, _, _ = select.select([master_fd], [], [], 0.5)
                    if not readable:
                        continue
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output += data
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                    if (b"M22_RESOURCE_STRESS_PASS" in output or
                            b"M22_RESOURCE_STRESS_FAILED" in output):
                        break
                    if b"panic" in output or b"Panic" in output:
                        break
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return output


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    output = run()
    (EVIDENCE / "serial.log").write_bytes(output)
    passes = output.count(b"M22_PASS")
    failures = output.count(b"M22_FAIL")
    rounds = output.count(b"baseline-restored")
    config = re.search(rb"M22_CONFIG rounds=(\d+)", output)
    expected_rounds = int(config.group(1)) if config else None
    print(
        f"\nM22 summary: passes={passes} failures={failures} "
        f"rounds={rounds}/{expected_rounds}"
    )
    if (b"M22_RESOURCE_STRESS_PASS" in output and failures == 0 and
            expected_rounds is not None and rounds == expected_rounds):
        print("M22_RESOURCE_STRESS_GATE_PASS")
        return 0
    print("M22_RESOURCE_STRESS_GATE_FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
