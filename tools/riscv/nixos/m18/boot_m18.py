#!/usr/bin/env python3
"""Boot the DRM kernel with the M18 initramfs (flipevent as /init) and
verify the atomic-modesetting ioctl surface.

Evidence protocol over the serial console:
  M18_PASS <check...> / M18_FAIL <check...> / M18_ALL_PASS / M18_FAILED

Artifacts (boot.ext4, initramfs, dtb, u-boot) live in target/drm-m18/,
built by tools/riscv/nixos/m18/build_m17.sh.
"""
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[4] / "target" / "drm-m18"
BOOTDISK = WORKDIR / "boot.ext4"
UBOOT = WORKDIR / "u-boot"

BOOTARGS = "console=ttyS0 loglevel=info init=/init"

CMDS = [
    (b"virtio scan", b"=>", 30),
    (b"ext4ls virtio 0:0 /", b"asterinas.booti", 10),
    (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 30),
    (b"ext4load virtio 0:0 0x88000000 /qemu-virt.dtb", b"bytes read", 10),
    (b"fdt addr 0x88000000", b"Working FDT set", 5),
    (b"fdt resize 0x1000", b"=>", 5),
    (b'setenv bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b'fdt set /chosen bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 30),
    (b"setenv initrd_size ${filesize}", b"=>", 5),
    (b"booti 0x80200000 0x83000000:${initrd_size} 0x88000000", b"Starting kernel", 30),
]


def run():
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "1G",
        # The DTB carries 4 CPUs; keep -smp in sync or the HSM hart-start
        # calls fail (DRM-M10 root cause).
        "-smp", "4",
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOTDISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-device",
    ]

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd,
                            stderr=slave_fd, close_fds=True)
    os.close(slave_fd)
    output = b""

    def read_until(pattern, timeout=10):
        nonlocal output
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                data = os.read(master_fd, 4096)
                if not data:
                    return False
                output += data
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                if pattern in output:
                    return True
        return False

    def send(cmd):
        os.write(master_fd, cmd + b"\r\n")

    if not read_until(b"=>", 30):
        print("[boot] U-Boot timeout")
        return output

    for cmd, expected, to in CMDS:
        send(cmd)
        time.sleep(0.3)
        if not read_until(expected, to):
            print(f"[boot] WARNING: '{cmd.decode()}' missed expected")
        if expected in output:
            output = output.split(expected, 1)[-1] if len(output.split(expected)) > 1 else b""

    print("[boot] Waiting for kernel (M18_ALL_PASS / M18_FAILED)...")
    deadline = time.time() + 180
    while time.time() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.5)
        if r:
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            output += data
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if b"M18_ALL_PASS" in output or b"M18_FAILED" in output:
                print("[boot] M18 verdict received")
                break
            if b"panic" in output or b"Panic" in output:
                print("[boot] PANIC!")
                break

    time.sleep(1)
    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.3)
            if not r:
                break
            data = os.read(master_fd, 4096)
            if not data:
                break
            output += data
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except OSError:
        pass

    os.close(master_fd)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    return output


def main():
    print("[boot] Launching QEMU (DRM kernel, M18 initramfs)...")
    output = run()
    text = output.decode("utf-8", errors="replace")
    print("\n=== M18 Atomic Modesetting Results ===")
    passes = failures = 0
    for line in text.split("\n"):
        if "M18_PASS" in line or "M18_FAIL" in line:
            # Strip kernel log interleaving: keep the M18_* payload.
            idx = line.find("M18_")
            print(f"  {line[idx:]}")
            passes += line.count("M18_PASS")
            failures += line.count("M18_FAIL")
    print(f"\nSummary: PASS={passes} FAIL={failures}")
    if b"M18_ALL_PASS" in output and failures == 0:
        print("M18_ATOMIC_PASS: all page-flip event checks passed")
        return 0
    print("M18_ATOMIC_FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
