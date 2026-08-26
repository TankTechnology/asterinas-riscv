#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the integrated RISC-V Xfce desktop on DRM or firmware fbdev."""

from __future__ import annotations

import argparse
import socket
import time
from enum import Enum
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
FB_PHYS_ADDR = 0x4000_0000
FB_APERTURE_SIZE = 4 * 1024 * 1024
FB_WIDTH_PIXELS = 1280
FB_HEIGHT_PIXELS = 800
FB_BYTES_PER_PIXEL = 4
FB_STRIDE_BYTES = FB_WIDTH_PIXELS * FB_BYTES_PER_PIXEL
FB_NODE_NAME = f"framebuffer@{FB_PHYS_ADDR:x}"
FB_NODE = f"/{FB_NODE_NAME}"
FB_SCREENSHOT = WORK / "fbdev-frame.ppm"

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
GRAPHICAL_TARGET = b"Reached target Asterinas DRM Xfce Desktop"
SESSION_STARTED = b"Started Xfce desktop session"
MODESETTING_DRIVER = b"modesetting_drv.so"
MODESETTING_ACTIVE = b"modeset(0): using default device"
FBDEV_DRIVER = b"fbdev_drv.so"
FBDEV_ACTIVE = b"FBDEV(0): using"
FBDEV_MODE = b"FBDEV(0): Virtual size is 1280x800"
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


class DisplayPath(Enum):
    VIRGL = "virgl"
    SOFTWARE_DRM = "software-drm"
    FBDEV = "fbdev"


def uboot_commands(display_path: DisplayPath) -> list[tuple[str, str, str, float]]:
    commands = [
        ("virtio-scan", "virtio scan", "=>", 30),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read", 60),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read", 30),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set", 10),
        ("dtb-resize", "fdt resize 0x1000", "=>", 10),
    ]
    if display_path is DisplayPath.FBDEV:
        commands.extend(
            [
                ("pci-probe", "pci display 0.1.0", "=>", 30),
                ("fb-node", f"fdt mknode / {FB_NODE_NAME}", "=>", 10),
                (
                    "fb-compatible",
                    f'fdt set {FB_NODE} compatible "simple-framebuffer"',
                    "=>",
                    10,
                ),
                (
                    "fb-reg",
                    f"fdt set {FB_NODE} reg "
                    f"<0x0 {FB_PHYS_ADDR:#x} 0x0 {FB_APERTURE_SIZE:#x}>",
                    "=>",
                    10,
                ),
                (
                    "fb-width",
                    f"fdt set {FB_NODE} width <{FB_WIDTH_PIXELS:#x}>",
                    "=>",
                    10,
                ),
                (
                    "fb-height",
                    f"fdt set {FB_NODE} height <{FB_HEIGHT_PIXELS:#x}>",
                    "=>",
                    10,
                ),
                (
                    "fb-stride",
                    f"fdt set {FB_NODE} stride <{FB_STRIDE_BYTES:#x}>",
                    "=>",
                    10,
                ),
                (
                    "fb-format",
                    f'fdt set {FB_NODE} format "x8r8g8b8"',
                    "=>",
                    10,
                ),
                ("fb-status", f'fdt set {FB_NODE} status "okay"', "=>", 10),
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


def has_visible_fbdev_frame(path: Path) -> bool:
    """Validate the guest framebuffer region of a QEMU PPM screendump."""

    try:
        magic, geometry, maximum, pixels = path.read_bytes().split(b"\n", 3)
        width, height = (int(value) for value in geometry.split())
    except (OSError, ValueError):
        return False
    expected_bytes = width * height * 3
    guest_frame_bytes = FB_WIDTH_PIXELS * FB_HEIGHT_PIXELS * 3
    return (
        magic == b"P6"
        and maximum == b"255"
        and width == FB_WIDTH_PIXELS
        and height >= FB_HEIGHT_PIXELS
        and len(pixels) == expected_bytes
        and any(pixels[:guest_frame_bytes])
    )


def capture_visible_fbdev_frame(path: Path) -> bool:
    """Capture the bochs scanout and reject a missing or entirely black frame."""

    path.unlink(missing_ok=True)
    if not MONITOR_SOCKET.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as monitor:
            monitor.connect(str(MONITOR_SOCKET))
            monitor.settimeout(5)
            monitor.sendall(f"screendump {path}\n".encode())
            time.sleep(2)
    except OSError:
        return False
    return has_visible_fbdev_frame(path)


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
        display_path = DisplayPath.FBDEV
        display = "gtk,gl=off,show-cursor=on" if args.interactive else "none"
        gpu_device = (
            f"bochs-display,xres={FB_WIDTH_PIXELS},yres={FB_HEIGHT_PIXELS}"
        )
    elif args.software_display:
        display_path = DisplayPath.SOFTWARE_DRM
        # The GTK OpenGL display path preserves the unused X byte of an XRGB
        # dumb buffer as alpha, making the guest surface transparent under
        # compositors.  The plain GTK/pixman path treats XRGB as opaque.
        display = "gtk,gl=off,show-cursor=on" if args.interactive else "none"
        gpu_device = "virtio-gpu-pci,id=gpu0"
    else:
        display_path = DisplayPath.VIRGL
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
    visible_fbdev_frame = False
    try:
        print("[boot] waiting for U-Boot", flush=True)
        boot.read_until(b"=> ", 60)
        for name, command, expected, timeout in uboot_commands(display_path):
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
                display_path is not DisplayPath.VIRGL
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
                clean = ANSI_RE.sub(b"", bytes(boot.transcript))
                if any(marker in clean for marker in PANIC_MARKERS):
                    reached = "panic-during-settle"
                    break
            else:
                reached = "settled"
            if reached == "settled" and display_path is DisplayPath.FBDEV:
                visible_fbdev_frame = capture_visible_fbdev_frame(FB_SCREENSHOT)
                print(
                    f"[frame] {FB_SCREENSHOT}: "
                    f"{'visible' if visible_fbdev_frame else 'missing-or-black'}",
                    flush=True,
                )
            if args.interactive and reached == "settled":
                print(
                    "[interactive] Xfce is ready in the QEMU GTK window; "
                    "press Ctrl-C to stop",
                    flush=True,
                )
                while boot.proc.poll() is None:
                    boot._drain(1.0)
    except (TimeoutError, RuntimeError, KeyboardInterrupt) as error:
        reached = "settle-error" if reached == "desktop-up" else "boot-error"
        print(f"[boot] {error}", flush=True)
    finally:
        boot.close()
        MONITOR_SOCKET.unlink(missing_ok=True)

    clean = ANSI_RE.sub(b"", bytes(boot.transcript))
    transcript = clean.decode("utf-8", "replace")
    display_driver = (
        FBDEV_DRIVER if display_path is DisplayPath.FBDEV else MODESETTING_DRIVER
    )
    display_active = (
        FBDEV_ACTIVE if display_path is DisplayPath.FBDEV else MODESETTING_ACTIVE
    )
    markers = {
        "graphical-target": GRAPHICAL_TARGET in clean,
        "xfce-session": SESSION_STARTED in clean,
        "display-driver": display_driver in clean,
        "display-active": display_active in clean,
        "graphics-mode": (
            display_path is not DisplayPath.VIRGL or GLAMOR_ACTIVE in clean.lower()
        ),
        "xorg-ready": XORG_READY in clean,
        "x11-connect": X11_CONNECT_OK in clean,
        "framebuffer": (
            visible_fbdev_frame and FBDEV_MODE in clean
            if display_path is DisplayPath.FBDEV
            else FRAMEBUFFER_ERROR not in clean
        ),
    }
    for name, present in markers.items():
        print(f"  {name}: {'OK' if present else 'MISSING'}")
    print(f"  reached: {reached}")
    print("\n=== serial tail ===")
    print(transcript[-5000:])

    passed = reached == "settled" and all(markers.values())
    if display_path is DisplayPath.FBDEV:
        print("XFCE_FBDEV_PASS" if passed else "XFCE_FBDEV_FAIL")
    else:
        print("XFCE_DRM_PASS" if passed else "XFCE_DRM_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
