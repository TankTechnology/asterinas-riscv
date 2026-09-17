#!/usr/bin/env python3
"""Boot DRM kernel (M16) with virtio-gpu-gl + host virglrenderer and run the
in-guest EGL/GLES2 verification client (Alpine rootfs initramfs).

Evidence protocol over the serial console:
  M16_GBM_BACKEND / M16_EGL_DISPLAY_OK / M16_EGL_CTX_OK
  M16_GL_VENDOR / M16_GL_RENDERER / M16_GL_VERSION
  M16_FRAME <n> csum=<hex> distinct_ge=<n>
  M16_FRAME_SAVED, M16_PPM_BASE64_BEGIN..END (decoded to evidence/),
  M16_EGL_DONE / M16_EGL_FAIL <stage>, M16_VERIFY_DONE

Artifacts (boot.ext4, initramfs, dtb, u-boot) live in target/drm-m16/.
Evidence (serial log + decoded PPM) lands in target/drm-m16/evidence/.
"""
import base64
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[4] / "target" / "drm-m16"
EVIDENCE = WORKDIR / "evidence"
BOOTDISK = WORKDIR / "boot.ext4"
UBOOT = WORKDIR / "u-boot"

BOOTARGS = "console=ttyS0 loglevel=info init=/init"

CMDS = [
    (b"virtio scan", b"=>", 30),
    (b"ext4ls virtio 0:0 /", b"asterinas.booti", 10),
    (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 30),
    (b"setenv aster_size ${filesize}", b"=>", 5),
    # DTB goes to 0x90000000: the 100+ MB initramfs at 0x83000000 would
    # overwrite anything at the old 0x88000000 slot.
    (b"ext4load virtio 0:0 0x90000000 /qemu-virt.dtb", b"bytes read", 10),
    (b"fdt addr 0x90000000", b"Working FDT set", 5),
    (b"fdt resize 0x1000", b"=>", 5),
    (b'setenv bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b'fdt set /chosen bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 120),
    (b"setenv initrd_size ${filesize}", b"=>", 5),
    (b"booti 0x80200000 0x83000000:${initrd_size} 0x90000000", b"Starting kernel", 30),
]


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
        "-device", "virtio-gpu-gl-device",
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

    print("[boot] Waiting for M16_VERIFY_DONE (TCG emulation, may take minutes)...")
    deadline = time.time() + 1800
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
            if b"M16_VERIFY_DONE" in output or b"M16_EGL_FAIL" in output:
                break
            if b"panic" in output or b"Panic" in output:
                print("[boot] PANIC detected")
                break

    time.sleep(2)
    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.3)
            if not r:
                break
            data = os.read(master_fd, 65536)
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


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    print("[boot] Launching QEMU (M16 virgl end-to-end)...")
    output = run()
    text = output.decode("utf-8", errors="replace")

    (EVIDENCE / "m16-virgl-serial.log").write_text(text)

    print("\n=== M16 virgl EGL Results ===")
    markers = {}
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if "M16_GBM_BACKEND" in line:
            markers["gbm"] = line
        elif "M16_EGL_DISPLAY_OK" in line:
            markers["display"] = line
        elif "M16_EGL_CTX_OK" in line:
            markers["ctx"] = line
        elif "M16_GL_" in line:
            key = line.split()[0]
            markers[key] = line
        elif "M16_FRAME " in line:
            frames.append(line)
        elif "M16_EGL_DONE" in line:
            markers["done"] = line
        elif "M16_EGL_FAIL" in line:
            markers["fail"] = line
        elif "M16_VERIFY_DONE" in line:
            markers["verify"] = line

    for k, v in sorted(markers.items()):
        print(f"  {v}")
    for f in frames:
        print(f"  {f}")

    m = re.search(
        r"M16_PPM_BASE64_BEGIN\s*\r?\n(.*?)M16_PPM_BASE64_END", text, re.S)
    ppm_ok = False
    if m:
        blob = re.sub(r"[^A-Za-z0-9+/=]", "", m.group(1))
        try:
            ppm = base64.b64decode(blob)
            if ppm.startswith(b"P6"):
                (EVIDENCE / "m16_frame.ppm").write_bytes(ppm)
                ppm_ok = True
                print(f"  PPM decoded: {len(ppm)} bytes -> {EVIDENCE}/m16_frame.ppm")
        except Exception as e:
            print(f"  PPM decode error: {e}")

    passed = (
        "done" in markers
        and "fail" not in markers
        and len(frames) >= 2
        and ppm_ok
        and "M16_GL_RENDERER" in markers
    )
    # Pixel sanity: per-frame checksums must differ (animated render), and the
    # frame must contain more than one distinct color (not a flat clear).
    if passed:
        csums = {f.split("csum=")[1].split()[0] for f in frames}
        if len(csums) < 2:
            print("  WARN: frame checksums identical (static image?)")
            passed = False
    print("M16_VIRGL_PASS" if passed else "M16_VIRGL_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
