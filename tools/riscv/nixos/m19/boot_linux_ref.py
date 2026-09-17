#!/usr/bin/env python3
"""A/B reference: boot stock Debian riscv64 Linux with the M19 initramfs.

Same userspace (Debian Mesa 25.0.7 + eglrender2 + ioctltrace) on a real
Linux kernel — tells us whether Mesa picks virgl there. If virgl works on
Linux but llvmpipe on Asterinas, the gap is in our ioctl surface; if
llvmpipe on both, it's the Debian Mesa packaging/selection.
"""
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
INITRAMFS = REPO / "target" / "drm-m19" / "initramfs.cpio.gz"
KERNEL = "/tmp/linux-rv/boot/vmlinux-6.12.94+deb13-riscv64"


def main():
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64",
        "-m", "2G",
        "-smp", "4",
        "-display", "egl-headless,gl=on",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", KERNEL,
        "-initrd", str(INITRAMFS),
        "-append", "console=ttyS0 init=/init",
        "-device", "virtio-gpu-gl-device",
    ]
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd,
                            stderr=slave_fd, close_fds=True)
    os.close(slave_fd)
    output = b""
    deadline = time.time() + 1200
    while time.time() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.5)
        if r:
            try:
                data = os.read(master_fd, 65536)
            except OSError:
                break
            if not data:
                break
            output += data
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if b"M19_VERIFY_DONE" in output:
                break
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    text = output.decode("utf-8", errors="replace")
    print("\n=== Linux A/B reference results ===")
    for line in text.split("\n"):
        if "M19_GL_RENDERER" in line or "M19_EGL" in line or "IOCTL VIRTGPU" in line:
            print(" ", line.strip())
    (REPO / "target" / "drm-m19" / "evidence" / "linux-ref-serial.log").write_text(text)


if __name__ == "__main__":
    main()
