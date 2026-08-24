#!/usr/bin/env python3
"""Boot the mini virgl rootfs (debug-instrumented Mesa) and capture the
MESA-DBG driver-selection prints.

This is the fast iteration harness for the "why does virgl not activate"
investigation. It repacks a dedicated boot disk with /tmp/mini-virgl2.cpio.gz
and boots it under virtio-gpu-gl-pci, then waits for the eglrender2 renderer
line (M19_GL_RENDERER) + the MESA-DBG / MINI_EGL_RC markers.

Log lands at /tmp/m20-mesavirgl3-stdout.log.
"""
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
KERNEL = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
DTB = REPO / "target/drm-m19/qemu-virt.dtb"
UBOOT = REPO / "target/drm-m19/u-boot"
MINI_CPIO = Path("/tmp/mini-virgl2.cpio.gz")

BOOT_DIR = Path("/tmp/mini-boot")
BOOTDISK = BOOT_DIR / "boot.ext4"
LOG = Path("/tmp/m20-mesavirgl3-stdout.log")

BOOTARGS = "console=ttyS0 loglevel=info init=/init"

CMDS = [
    (b"virtio scan", b"=>", 30),
    (b"ext4ls virtio 0:0 /", b"asterinas.booti", 10),
    (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 30),
    (b"setenv aster_size ${filesize}", b"=>", 5),
    (b"ext4load virtio 0:0 0x90000000 /qemu-virt.dtb", b"bytes read", 10),
    (b"fdt addr 0x90000000", b"Working FDT set", 5),
    (b"fdt resize 0x1000", b"=>", 5),
    (b'setenv bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b'fdt set /chosen bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 120),
    (b"setenv initrd_size ${filesize}", b"=>", 5),
    (b"booti 0x80200000 0x83000000:${initrd_size} 0x90000000", b"Starting kernel", 30),
]


def build_bootdisk():
    BOOT_DIR.mkdir(parents=True, exist_ok=True)
    stage = BOOT_DIR / ".stage"
    if stage.exists():
        subprocess.run(["rm", "-rf", str(stage)])
    stage.mkdir(parents=True)
    subprocess.run(["cp", str(KERNEL), str(stage / "asterinas.booti")], check=True)
    subprocess.run(["cp", str(MINI_CPIO), str(stage / "initramfs.cpio.gz")], check=True)
    subprocess.run(["cp", str(DTB), str(stage / "qemu-virt.dtb")], check=True)
    if BOOTDISK.exists():
        BOOTDISK.unlink()
    subprocess.run(["truncate", "-s", "64M", str(BOOTDISK)], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-d", str(stage), str(BOOTDISK)], check=True)
    subprocess.run(["rm", "-rf", str(stage)])
    print(f"[boot] boot disk repacked: {BOOTDISK} (initramfs = {MINI_CPIO.name})")


def run() -> bytes:
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "4",
        "-display", "egl-headless,gl=on",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOTDISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-gl-pci",
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
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    return False
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

    if not read_until(b"=>", 60):
        print("[boot] U-Boot timeout")
        return output

    for cmd, expected, to in CMDS:
        send(cmd)
        time.sleep(0.3)
        if not read_until(expected, to):
            print(f"[boot] WARNING: '{cmd.decode()}' missed expected")

    print("[boot] Waiting for renderer marker (TCG, may take minutes)...")
    deadline = time.time() + 900
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
            if b"MINI_EGL_RC" in output:
                break
            if b"panic" in output or b"Panic" in output:
                print("[boot] PANIC detected")
                break

    time.sleep(1)
    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    return output


def main() -> int:
    build_bootdisk()
    print("[boot] Launching QEMU (mini virgl debug)...")
    output = run()
    text = output.decode("utf-8", errors="replace")
    LOG.write_text(text)

    print("\n=== MESA-DBG + renderer results ===")
    for line in text.splitlines():
        s = line.strip()
        if ("MESA-DBG" in s or "M19_GL_" in s or "MINI_EGL_RC" in s
                or "MESA-LOADER" in s or "using driver" in s
                or "pipe_loader" in s or "VIRTGPU" in s):
            print(f"  {s}")
    print(f"\n[full log] {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
