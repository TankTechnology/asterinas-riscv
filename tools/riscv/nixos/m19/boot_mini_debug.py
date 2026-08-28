#!/usr/bin/env python3
"""Boot the mini virgl rootfs (debug-instrumented Mesa) and capture the
MESA-DBG driver-selection prints.

This is the fast iteration harness for the "why does virgl not activate"
investigation. It repacks a dedicated boot disk with /tmp/mini-virgl2.cpio.gz
and boots it under virtio-gpu-gl-pci, then waits for the eglrender2 renderer
line (M19_GL_RENDERER) + the MESA-DBG / MINI_EGL_RC markers.

Log lands at /tmp/m20-mesavirgl3-stdout.log.
"""
import argparse
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
KERNEL = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
DTB = REPO / "target/drm-m19/qemu-virt.dtb"
UBOOT = REPO / "target/drm-m19/u-boot"
MINI_CPIO = Path("/tmp/mini-virgl2.cpio.gz")
MINI_ROOT_DISK = Path("/tmp/mini-virgl2.ext2")

parser = argparse.ArgumentParser(description="Run the RISC-V DRM/Mesa mini-root gate")
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--raw-only", action="store_true", help="stop after the raw virgl test")
mode.add_argument("--public-only", action="store_true", help="stop after modetest and kmscube")
args = parser.parse_args()
RAW_ONLY = args.raw_only
PUBLIC_ONLY = args.public_only

BOOT_DIR = Path("/tmp/mini-boot")
BOOTDISK = BOOT_DIR / "boot.ext4"
LOG = Path("/tmp/m20-mesavirgl3-stdout.log")

# The ext2-backed Mesa closure performs many small reads. Keep routine block
# I/O logs off the serial console so they do not dominate TCG test time.
BOOTARGS = "console=ttyS0 loglevel=warn init=/init"

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
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    subprocess.run(["cp", str(KERNEL), str(stage / "asterinas.booti")], check=True)
    subprocess.run(["cp", str(MINI_CPIO), str(stage / "initramfs.cpio.gz")], check=True)
    subprocess.run(["cp", str(DTB), str(stage / "qemu-virt.dtb")], check=True)
    if BOOTDISK.exists():
        BOOTDISK.unlink()
    subprocess.run(["truncate", "-s", "256M", str(BOOTDISK)], check=True)
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
    ]
    if MINI_ROOT_DISK.exists():
        argv.extend((
            "-drive", f"if=none,format=raw,file={MINI_ROOT_DISK},id=mesaroot",
            "-device", "virtio-blk-device,drive=mesaroot",
        ))
    argv.extend(("-device", "virtio-gpu-gl-pci"))

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd,
                            stderr=slave_fd, close_fds=True)
    os.close(slave_fd)
    output = b""

    def read_until(pattern, timeout=10):
        nonlocal output
        start = len(output)
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
                if pattern in output[start:]:
                    return True
        return False

    def send(cmd):
        os.write(master_fd, cmd + b"\r\n")

    def cleanup():
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait()

    if not read_until(b"=>", 60):
        print("[boot] U-Boot timeout")
        cleanup()
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
            if PUBLIC_ONLY:
                completion_marker = b"MINI_PUBLIC_KMS_DONE"
            elif RAW_ONLY:
                completion_marker = b"MINI_RAW_DONE"
            else:
                completion_marker = b"MINI_EGL_DONE"
            if completion_marker in output:
                break
            if b"panic" in output or b"Panic" in output:
                print("[boot] PANIC detected")
                break

    time.sleep(1)
    cleanup()
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
                or "pipe_loader" in s or "VIRTGPU" in s or "MINI_PRIME" in s
                or "M20_PRIME" in s or "MINI_RAW_RC" in s):
            print(f"  {s}")
    print(f"\n[full log] {LOG}")
    required_markers = [
        "MINI_PUBLIC_KMS_DONE",
        "MINI_MODETEST_ENUM_BEGIN",
        "MINI_MODETEST_ENUM_RC=0",
        "MINI_MODETEST_LEGACY_BEGIN",
        "MINI_MODETEST_LEGACY_RC=0",
        "MINI_MODETEST_ATOMIC_BEGIN",
        "MINI_MODETEST_ATOMIC_RC=0",
        "MINI_KMSCUBE_LEGACY_BEGIN",
        "MINI_KMSCUBE_LEGACY_RC=0",
        "MINI_KMSCUBE_ATOMIC_BEGIN",
        "MINI_KMSCUBE_ATOMIC_RC=0",
        "Rendered 4 frames",
        'renderer: "virgl (',
        "Connectors:",
        "CRTCs:",
        "Planes:",
    ]
    if not PUBLIC_ONLY:
        required_markers.extend((
            "M20_PRIME_PASS",
            "MINI_PRIME_RC=0",
            "M16_VIRGL_RAW_PASS",
            "MINI_RAW_RC=0",
        ))
    if not RAW_ONLY and not PUBLIC_ONLY:
        required_markers.extend((
            "M19_GL_RENDERER virgl",
            "M19_VBLANK_UAPI_PASS",
            "M19_VBLANK_INACTIVE_PASS",
            "M19_FRAMES_DISTINCT 4",
            "M19_EGL_DONE",
            "MINI_EGL_RC=0",
        ))
    forbidden_markers = [
        "M20_PRIME_FAIL",
        "M16_VIRGL_FAIL",
        "M19_EGL_FAIL",
        "M19_GL_RENDERER llvmpipe",
        "M19_GL_RENDERER softpipe",
        'renderer: "llvmpipe',
        'renderer: "softpipe',
        "failed to commit:",
        "no atomic modesetting support",
        "Illegal resource",
        "illegal resource",
        "error decoding command",
    ]
    return_codes = re.findall(
        r"MINI_(?:MODETEST_ENUM|MODETEST_LEGACY|MODETEST_ATOMIC|"
        r"KMSCUBE_LEGACY|KMSCUBE_ATOMIC|PRIME|RAW|EGL)_RC=(-?\d+)",
        text,
    )
    kmscube_reports = len(re.findall(r"Rendered 4 frames", text))
    kmscube_virgl_reports = len(re.findall(r'renderer: "virgl \(', text))
    passed = (
        all(marker in text for marker in required_markers)
        and not any(marker in text for marker in forbidden_markers)
        and all(code == "0" for code in return_codes)
        and kmscube_reports == 2
        and kmscube_virgl_reports == 2
        and "panic" not in text.lower()
    )
    print("MINI_VIRGL_PASS" if passed else "MINI_VIRGL_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
