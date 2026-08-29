#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the integrated RISC-V Xfce desktop on DRM or firmware fbdev."""

from __future__ import annotations

import argparse
import math
import re
import signal
import socket
import time
from enum import Enum
from pathlib import Path

from boot_xfce_desktop import ANSI_RE, Boot
from build_xfce_drm import QEMU_CPU, STAGE1_INITRAMFS, pack_boot_disk


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
FB_APERTURE_SIZE_BYTES = 4 * 1024 * 1024
FB_WIDTH_PIXELS = 1280
FB_HEIGHT_PIXELS = 800
FB_BYTES_PER_PIXEL = 4
FB_STRIDE_BYTES = FB_WIDTH_PIXELS * FB_BYTES_PER_PIXEL
FB_NODE_NAME = f"framebuffer@{FB_PHYS_ADDR:x}"
FB_NODE = f"/{FB_NODE_NAME}"
FB_SCREENSHOT = WORK / "fbdev-frame.ppm"
PPM_RGB_BYTES_PER_PIXEL = 3
MIN_VISIBLE_PIXELS_PERCENT = 1
MIN_VISIBLE_SPAN_PERCENT = 10
MONITOR_TIMEOUT_SECONDS = 5
MONITOR_PROMPT = b"(qemu)"

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
GRAPHICAL_TARGET = b"Reached target Asterinas DRM Xfce Desktop"
SESSION_STARTED = b"Started Xfce desktop session"
MODESETTING_DRIVER = b"modesetting_drv.so"
MODESETTING_ACTIVE = b"modeset(0): using default device"
FBDEV_DRIVER = b"fbdev_drv.so"
FBDEV_ACTIVE = b"FBDEV(0): using"
FBDEV_MODE = (
    f"FBDEV(0): Virtual size is {FB_WIDTH_PIXELS}x{FB_HEIGHT_PIXELS}"
).encode()
GLAMOR_VIRGL_ACTIVE = b"glamor x acceleration enabled on virgl"
MESA_DRI3_ACTIVE = b"Using DRI3 for screen 0"
GL_BENCH_PASS = b"XFCE_GL_BENCH_PASS"
GL_DIRECT = b"XFCE_GL_DIRECT yes"
GL_RENDERER_VIRGL = b"XFCE_GL_RENDERER virgl"
GL_RENDERER_SOFTWARE = b"XFCE_GL_RENDERER llvmpipe"
XORG_READY = b"XFCE_DRM_XORG_READY"
X11_CONNECT_OK = b"XFCE_DRM_X11_CONNECT_OK"
PANIC_MARKERS = (
    b"kernel panic",
    b"Kernel panic",
    b"Uncaught panic",
    b"panic!",
)
BUG_MARKER_RE = re.compile(rb"(?<![A-Za-z])BUG:")
FRAMEBUFFER_ERROR = b"failed to add fb"
VIRGL_CONTEXT_ERRORS = (
    b"Illegal resource",
    b"failed to dispatch CREATE_OBJECT",
    b"Illegal command buffer",
)


class DisplayPath(Enum):
    VIRGL = "virgl"
    SOFTWARE_DRM = "software-drm"
    FBDEV = "fbdev"


class BootOutcome(Enum):
    TIMEOUT = "timeout"
    PANIC = "panic"
    DESKTOP_UP = "desktop-up"
    PANIC_DURING_SETTLE = "panic-during-settle"
    SETTLED = "settled"
    SETTLE_ERROR = "settle-error"
    BOOT_ERROR = "boot-error"
    INTERRUPTED = "interrupted"


def positive_seconds(value: str) -> float:
    """Parse a finite, positive command-line duration in seconds."""

    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid duration: {value}") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and positive")
    return seconds


def handle_termination_signal(_signum: int, _frame: object) -> None:
    """Converts SIGTERM into the normal QEMU-cleanup path."""

    raise KeyboardInterrupt


def contains_panic(output: bytes) -> bool:
    return any(marker in output for marker in PANIC_MARKERS) or bool(
        BUG_MARKER_RE.search(output)
    )


def renderer_is_ready(display_path: DisplayPath, output: bytes) -> bool:
    if display_path is DisplayPath.FBDEV:
        return True
    expected_renderer = (
        GL_RENDERER_VIRGL
        if display_path is DisplayPath.VIRGL
        else GL_RENDERER_SOFTWARE
    )
    return (
        GL_BENCH_PASS in output
        and GL_DIRECT in output
        and (display_path is not DisplayPath.VIRGL or MESA_DRI3_ACTIVE in output)
        and expected_renderer in output
    )


def uboot_commands(
    display_path: DisplayPath, kernel_loglevel: str
) -> list[tuple[str, str, str, float]]:
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
                    f"<0x0 {FB_PHYS_ADDR:#x} 0x0 {FB_APERTURE_SIZE_BYTES:#x}>",
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
            (
                "bootargs",
                f'setenv bootargs "console=ttyS0 loglevel={kernel_loglevel} init=/init"',
                "=>",
                10,
            ),
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
    """Validate substantial guest content in QEMU's binary PPM screendump."""

    # QEMU's `screendump` emits Netpbm P6: an ASCII header followed by one
    # three-byte RGB tuple per pixel.  See docs/interop/vnc.txt in QEMU and
    # https://netpbm.sourceforge.net/doc/ppm.html.
    try:
        magic, geometry, maximum, pixels = path.read_bytes().split(b"\n", 3)
        width, height = (int(value) for value in geometry.split())
    except (OSError, ValueError):
        return False
    expected_bytes = width * height * PPM_RGB_BYTES_PER_PIXEL
    if (
        magic != b"P6"
        or maximum != b"255"
        or width != FB_WIDTH_PIXELS
        or height < FB_HEIGHT_PIXELS
        or len(pixels) != expected_bytes
    ):
        return False

    nonblack_pixels = 0
    min_x = FB_WIDTH_PIXELS
    max_x = -1
    min_y = FB_HEIGHT_PIXELS
    max_y = -1
    guest_frame_bytes = (
        FB_WIDTH_PIXELS * FB_HEIGHT_PIXELS * PPM_RGB_BYTES_PER_PIXEL
    )
    for offset in range(0, guest_frame_bytes, PPM_RGB_BYTES_PER_PIXEL):
        if pixels[offset] or pixels[offset + 1] or pixels[offset + 2]:
            pixel_index = offset // PPM_RGB_BYTES_PER_PIXEL
            x = pixel_index % FB_WIDTH_PIXELS
            y = pixel_index // FB_WIDTH_PIXELS
            nonblack_pixels += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)

    total_guest_pixels = FB_WIDTH_PIXELS * FB_HEIGHT_PIXELS
    minimum_visible_pixels = (
        total_guest_pixels * MIN_VISIBLE_PIXELS_PERCENT // 100
    )
    minimum_x_span = FB_WIDTH_PIXELS * MIN_VISIBLE_SPAN_PERCENT // 100
    minimum_y_span = FB_HEIGHT_PIXELS * MIN_VISIBLE_SPAN_PERCENT // 100
    return (
        nonblack_pixels >= minimum_visible_pixels
        and max_x - min_x + 1 >= minimum_x_span
        and max_y - min_y + 1 >= minimum_y_span
    )


def read_monitor_prompt(monitor: socket.socket) -> bytes:
    response = bytearray()
    while MONITOR_PROMPT not in response:
        chunk = monitor.recv(4096)
        if not chunk:
            raise OSError("QEMU monitor closed before returning its prompt")
        response.extend(chunk)
    return bytes(response)


def capture_visible_fbdev_frame(path: Path) -> bool:
    """Capture the bochs scanout and reject a missing or entirely black frame."""

    path.unlink(missing_ok=True)
    if not MONITOR_SOCKET.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as monitor:
            monitor.connect(str(MONITOR_SOCKET))
            monitor.settimeout(MONITOR_TIMEOUT_SECONDS)
            read_monitor_prompt(monitor)
            monitor.sendall(f"screendump {path}\n".encode())
            read_monitor_prompt(monitor)
    except OSError:
        return False
    return has_visible_fbdev_frame(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=positive_seconds, default=900.0)
    parser.add_argument("--settle", type=positive_seconds, default=15.0)
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
    parser.add_argument(
        "--kernel-image",
        type=Path,
        default=REPO / "target/osdk/aster-kernel-osdk-bin.Image",
        help="RISC-V Linux Image to place in the temporary boot disk",
    )
    parser.add_argument(
        "--root-disk",
        type=Path,
        default=ROOT_DISK,
        help="persistent root image to attach to the guest",
    )
    parser.add_argument(
        "--serial-log",
        type=Path,
        default=WORK / "serial.log",
        help="path for the complete serial transcript",
    )
    parser.add_argument(
        "--kernel-loglevel",
        choices=("warn", "info", "debug"),
        default="warn",
        help="kernel log level used for boot diagnostics",
    )
    args = parser.parse_args()

    if args.software_display and args.fbdev_display:
        parser.error("--software-display and --fbdev-display are mutually exclusive")

    # Keep the boot image and DTB synchronized with the current kernel, CPU
    # page-table mode, memory size, and requested hart count.
    pack_boot_disk(STAGE1_INITRAMFS, args.smp, args.kernel_image)
    for path in (UBOOT, BOOT_DISK, args.root_disk):
        if not path.exists():
            raise SystemExit(f"missing {path}; run build_xfce_drm.py first")
    args.serial_log.parent.mkdir(parents=True, exist_ok=True)
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
        "-cpu", QEMU_CPU,
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", display,
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={args.root_disk},id=rootdisk",
        "-device", "virtio-blk-device,drive=rootdisk",
        "-device", gpu_device,
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-serial", "stdio",
        "-monitor", f"unix:{MONITOR_SOCKET},server=on,wait=off",
    ]

    boot = Boot(argv, args.serial_log)
    reached = BootOutcome.TIMEOUT
    visible_fbdev_frame = False
    try:
        print("[boot] waiting for U-Boot", flush=True)
        boot.read_until(b"=> ", 60)
        for name, command, expected, timeout in uboot_commands(
            display_path, args.kernel_loglevel
        ):
            print(f"[uboot] {name}", flush=True)
            boot.send(command)
            boot.read_until(expected.encode(), timeout)
            if name != "boot" and expected != "=>":
                boot.read_until(b"=> ", 30)

        boot.read_until(INIT_MARKER, 300)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            clean = ANSI_RE.sub(b"", bytes(boot.pending))
            if contains_panic(clean):
                reached = BootOutcome.PANIC
                break
            graphics_ready = (
                display_path is not DisplayPath.VIRGL
                or GLAMOR_VIRGL_ACTIVE in clean.lower()
            )
            renderer_ready = renderer_is_ready(display_path, clean)
            if (
                GRAPHICAL_TARGET in clean
                and SESSION_STARTED in clean
                and graphics_ready
                and renderer_ready
                and X11_CONNECT_OK in clean
            ):
                reached = BootOutcome.DESKTOP_UP
                break
            boot._drain(1.0)

        if reached is BootOutcome.DESKTOP_UP:
            settle_transcript_start = len(boot.transcript)
            settle_deadline = time.monotonic() + args.settle
            while time.monotonic() < settle_deadline:
                boot._drain(1.0)
                settle_output = ANSI_RE.sub(
                    b"", bytes(boot.transcript[settle_transcript_start:])
                )
                if contains_panic(settle_output):
                    reached = BootOutcome.PANIC_DURING_SETTLE
                    break
            else:
                reached = BootOutcome.SETTLED
            if reached is BootOutcome.SETTLED and display_path is DisplayPath.FBDEV:
                visible_fbdev_frame = capture_visible_fbdev_frame(FB_SCREENSHOT)
                print(
                    f"[frame] {FB_SCREENSHOT}: "
                    f"{'visible' if visible_fbdev_frame else 'missing-or-black'}",
                    flush=True,
                )
            if args.interactive and reached is BootOutcome.SETTLED:
                print(
                    "[interactive] Xfce is ready in the QEMU GTK window; "
                    "press Ctrl-C to stop",
                    flush=True,
                )
                while boot.proc.poll() is None:
                    boot._drain(1.0)
    except KeyboardInterrupt:
        if reached is not BootOutcome.SETTLED:
            reached = BootOutcome.INTERRUPTED
        print("[boot] interrupted", flush=True)
    except (TimeoutError, RuntimeError) as error:
        reached = (
            BootOutcome.SETTLE_ERROR
            if reached is BootOutcome.DESKTOP_UP
            else BootOutcome.BOOT_ERROR
        )
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
            display_path is not DisplayPath.VIRGL
            or GLAMOR_VIRGL_ACTIVE in clean.lower()
        ),
        "renderer": renderer_is_ready(display_path, clean),
        "xorg-ready": XORG_READY in clean,
        "x11-connect": X11_CONNECT_OK in clean,
        "framebuffer": (
            visible_fbdev_frame and FBDEV_MODE in clean
            if display_path is DisplayPath.FBDEV
            else FRAMEBUFFER_ERROR not in clean
        ),
        "command-stream": (
            display_path is not DisplayPath.VIRGL
            or not any(marker in clean for marker in VIRGL_CONTEXT_ERRORS)
        ),
    }
    for name, present in markers.items():
        print(f"  {name}: {'OK' if present else 'MISSING'}")
    print(f"  reached: {reached.value}")
    print("\n=== serial tail ===")
    print(transcript[-5000:])

    passed = reached is BootOutcome.SETTLED and all(markers.values())
    if display_path is DisplayPath.FBDEV:
        print("XFCE_FBDEV_PASS" if passed else "XFCE_FBDEV_FAIL")
    else:
        print("XFCE_DRM_PASS" if passed else "XFCE_DRM_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_termination_signal)
    raise SystemExit(main())
