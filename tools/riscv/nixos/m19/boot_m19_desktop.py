#!/usr/bin/env python3
"""Boot the M19 Debian rootfs with a GTK window — desktop demo.

kmscube spins a cube on the DRM scanout; the QEMU window shows it live.
Close the window or Ctrl-C this script to stop. Serial console is mirrored
to the terminal (busybox shell available there).
"""
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[4] / "target" / "drm-m19"
BOOTDISK = WORKDIR / "boot.ext4"
UBOOT = WORKDIR / "u-boot"

BOOTARGS = "console=ttyS0 loglevel=4 init=/init"

CMDS = [
    (b"virtio scan", b"=>", 30),
    (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 60),
    (b"ext4load virtio 0:0 0x90000000 /qemu-virt.dtb", b"bytes read", 30),
    (b"fdt addr 0x90000000", b"Working FDT set", 10),
    (b"fdt resize 0x1000", b"=>", 10),
    (b'setenv bootargs "' + BOOTARGS.encode() + b'"', b"=>", 10),
    (b'fdt set /chosen bootargs "' + BOOTARGS.encode() + b'"', b"=>", 10),
    (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 180),
    (b"setenv initrd_size ${filesize}", b"=>", 10),
    (b"booti 0x80200000 0x83000000:${initrd_size} 0x90000000", b"Starting kernel", 60),
]


def main():
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "4",
        "-display", "gtk,gl=on",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOTDISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-gl-device",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-mouse-device",
    ]

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd,
                            stderr=slave_fd, close_fds=True)
    os.close(slave_fd)

    def send(cmd):
        os.write(master_fd, cmd + b"\r\n")

    output = b""
    for cmd, expected, to in CMDS:
        deadline = time.time() + to
        while time.time() < deadline and expected not in output:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                data = os.read(master_fd, 65536)
                if not data:
                    break
                output += data
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        send(cmd)
        time.sleep(0.2)

    print("\n[desktop] kernel is booting; the QEMU window shows the display output.")
    print("[desktop] kmscube starts automatically (TCG emulation: expect ~0.3 fps).")
    print("[desktop] close the QEMU window or Ctrl-C here to stop.\n")

    # Stream the serial console until QEMU exits or we are interrupted.
    try:
        while proc.poll() is None:
            r, _, _ = select.select([master_fd], [], [], 0.5)
            if r:
                data = os.read(master_fd, 65536)
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
