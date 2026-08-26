#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the integrated RISC-V Xfce desktop on DRM or firmware fbdev."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from boot_xfce_desktop import ANSI_RE, Boot


REPO = Path(__file__).resolve().parents[3]
WORK = REPO / "target/xfce-drm"
UBOOT = WORK / "u-boot"
BOOT_DISK = WORK / "boot.ext4"
ROOT_DISK = WORK / "root.ext2"
MONITOR_SOCKET = WORK / "qemu-monitor.sock"
KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8400_0000
DTB_LOAD = 0xB000_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
GRAPHICAL_TARGET = b"Reached target Asterinas DRM Xfce Desktop"
SESSION_STARTED = b"Started Xfce desktop session"
MODESETTING_DRIVER = b"modesetting_drv.so"
MODESETTING_ACTIVE = b"modeset(0): using default device"
FBDEV_DRIVER = b"fbdev_drv.so"
FBDEV_ACTIVE = b"FBDEV(0): using"
GLAMOR_ACTIVE = b"glamor x acceleration enabled"
XORG_READY = b"XFCE_DRM_XORG_READY"
X11_CONNECT_OK = b"XFCE_DRM_X11_CONNECT_OK"
PANIC_MARKERS = (
    b"kernel panic",
    b"Kernel panic",
    b"Uncaught panic",
    b"panic!",
    b"BUG:",
)
FRAMEBUFFER_ERROR = b"failed to add fb"


def uboot_commands(fbdev_display: bool) -> list[tuple[str, str, str, float]]:
    commands = [
        ("virtio-scan", "virtio scan", "=>", 30),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read", 60),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read", 30),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set", 10),
        ("dtb-resize", "fdt resize 0x1000", "=>", 10),
    ]
    if fbdev_display:
        commands.extend(
            [
                ("pci-probe", "pci display 0.1.0", "=>", 30),
                ("fb-node", "fdt mknode / framebuffer@40000000", "=>", 10),
                (
                    "fb-compatible",
                    'fdt set /framebuffer@40000000 compatible "simple-framebuffer"',
                    "=>",
                    10,
                ),
                (
                    "fb-reg",
                    "fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x400000>",
                    "=>",
                    10,
                ),
                ("fb-width", "fdt set /framebuffer@40000000 width <0x500>", "=>", 10),
                ("fb-height", "fdt set /framebuffer@40000000 height <0x320>", "=>", 10),
                ("fb-stride", "fdt set /framebuffer@40000000 stride <0x1400>", "=>", 10),
                (
                    "fb-format",
                    'fdt set /framebuffer@40000000 format "x8r8g8b8"',
                    "=>",
                    10,
                ),
                ("fb-status", 'fdt set /framebuffer@40000000 status "okay"', "=>", 10),
            ]
        )
    commands.extend(
        [
            ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>", 10),
            (
                "initrd-load",
                f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz",
                "bytes read",
                240,
            ),
            ("initrd-size", "setenv initrd_size ${filesize}", "=>", 10),
            (
                "boot",
                f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
                "Starting kernel",
                60,
            ),
        ]
    )
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--settle", type=float, default=15.0)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument(
        "--software-display",
        action="store_true",
        help="accept the CPU-rendered modesetting fallback without glamor",
    )
    parser.add_argument(
        "--fbdev-display",
        action="store_true",
        help="use bochs-display and Xorg fbdev without a DRM device",
    )
    parser.add_argument("--smp", type=int, default=4)
    args = parser.parse_args()

    if args.software_display and args.fbdev_display:
        parser.error("--software-display and --fbdev-display are mutually exclusive")

    for path in (UBOOT, BOOT_DISK, ROOT_DISK):
        if not path.exists():
            raise SystemExit(f"missing {path}; run build_xfce_drm.py first")
    serial_log = WORK / "serial.log"
    MONITOR_SOCKET.unlink(missing_ok=True)

    if args.fbdev_display:
        display = "gtk,gl=off,show-cursor=on" if args.interactive else "none"
        gpu_device = "bochs-display,xres=1280,yres=800"
    elif args.software_display:
        # The GTK OpenGL display path preserves the unused X byte of an XRGB
        # dumb buffer as alpha, making the guest surface transparent under
        # compositors.  The plain GTK/pixman path treats XRGB as opaque.
        display = "gtk,gl=off,show-cursor=on" if args.interactive else "none"
        gpu_device = "virtio-gpu-pci,id=gpu0"
    else:
        display = "gtk,gl=on,show-cursor=on" if args.interactive else "egl-headless,gl=on"
        gpu_device = "virtio-gpu-gl-pci,id=gpu0"

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", display,
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={ROOT_DISK},id=rootdisk",
        "-device", "virtio-blk-device,drive=rootdisk",
        "-device", gpu_device,
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-serial", "stdio",
        "-monitor", f"unix:{MONITOR_SOCKET},server=on,wait=off",
    ]

    boot = Boot(argv, serial_log)
    reached = "timeout"
    try:
        print("[boot] waiting for U-Boot", flush=True)
        boot.read_until(b"=> ", 60)
        for name, command, expected, timeout in uboot_commands(args.fbdev_display):
            print(f"[uboot] {name}", flush=True)
            boot.send(command)
            boot.read_until(expected.encode(), timeout)
            if name != "boot" and expected != "=>":
                boot.read_until(b"=> ", 30)

        boot.read_until(INIT_MARKER, 300)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            clean = ANSI_RE.sub(b"", bytes(boot.pending))
            if any(marker in clean for marker in PANIC_MARKERS):
                reached = "panic"
                break
            graphics_ready = (
                args.software_display
                or args.fbdev_display
                or GLAMOR_ACTIVE in clean.lower()
            )
            if (
                GRAPHICAL_TARGET in clean
                and SESSION_STARTED in clean
                and graphics_ready
                and X11_CONNECT_OK in clean
            ):
                reached = "desktop-up"
                break
            boot._drain(1.0)

        if reached == "desktop-up":
            settle_deadline = time.monotonic() + args.settle
            while time.monotonic() < settle_deadline:
                boot._drain(1.0)
            if args.interactive:
                print("[interactive] Xfce is ready in the QEMU GTK window; press Ctrl-C to stop", flush=True)
                while boot.proc.poll() is None:
                    boot._drain(1.0)
    except (TimeoutError, RuntimeError, KeyboardInterrupt) as error:
        print(f"[boot] {error}", flush=True)
    finally:
        boot.close()
        MONITOR_SOCKET.unlink(missing_ok=True)

    clean = ANSI_RE.sub(b"", bytes(boot.transcript))
    transcript = clean.decode("utf-8", "replace")
    display_driver = FBDEV_DRIVER if args.fbdev_display else MODESETTING_DRIVER
    display_active = FBDEV_ACTIVE if args.fbdev_display else MODESETTING_ACTIVE
    markers = {
        "graphical-target": GRAPHICAL_TARGET in clean,
        "xfce-session": SESSION_STARTED in clean,
        "display-driver": display_driver in clean,
        "display-active": display_active in clean,
        "graphics-mode": (
            args.software_display
            or args.fbdev_display
            or GLAMOR_ACTIVE in clean.lower()
        ),
        "xorg-ready": XORG_READY in clean,
        "x11-connect": X11_CONNECT_OK in clean,
        "framebuffer": args.fbdev_display or FRAMEBUFFER_ERROR not in clean,
    }
    for name, present in markers.items():
        print(f"  {name}: {'OK' if present else 'MISSING'}")
    print(f"  reached: {reached}")
    print("\n=== serial tail ===")
    print(transcript[-5000:])

    passed = reached == "desktop-up" and all(markers.values())
    if args.fbdev_display:
        print("XFCE_FBDEV_PASS" if passed else "XFCE_FBDEV_FAIL")
    else:
        print("XFCE_DRM_PASS" if passed else "XFCE_DRM_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
