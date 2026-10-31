#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the browser-web image directly and verify a real Firefox desktop.

This is the compact replacement for the historical U-Boot/M3/M4 gate stack.
The kernel loads Stage1 as an initrd, Stage1 mounts the browser root, and the
serial transcript must reach the ordered desktop-session markers.  A guest
observer then proves that a visible Firefox window exists, and the host reads
the RAM-backed guest framebuffer through the QEMU monitor.  Marker-only boot
evidence is not enough: the original direct boot pointed the simple-framebuffer
at an unassigned PCIe BAR, so every marker passed while the guest rendered into
a black hole.

The DTB must describe a RAM-backed simple-framebuffer.  The Makefile target
builds that variant from the QEMU bochs DTB before invoking this gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.riscv.debian.direct_boot_gate import Serial

DESKTOP_MARKERS = (
    "BROWSER_WEB_DESKTOP_STAGE=display-provider-ready",
    "BROWSER_WEB_DESKTOP_STAGE=xorg-start",
    "BROWSER_WEB_DESKTOP_STAGE=x-socket-ready",
    "BROWSER_WEB_DESKTOP_STAGE=window-manager-start",
    "BROWSER_WEB_DESKTOP_STAGE=desktop-start",
    "BROWSER_WEB_DESKTOP_STAGE=panel-start",
)

DESKTOP_FRAME = re.compile(r"BROWSER_WEB_DESKTOP_FRAME_STAGE=desktop")
FIREFOX_WINDOW = re.compile(
    r"BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=ready id=(\S+) title=(\S+)\r?\n"
)
FRAME_READY = re.compile(r"BROWSER_WEB_DESKTOP_FRAME_READY source=host-pmemsave")
EVIDENCE_FAILURES = (
    "BROWSER_WEB_DESKTOP_FRAME_STAGE=failed",
    "BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=timeout",
    "BROWSER_WEB_DESKTOP_FRAME_READY=failed",
    "Failed to start asterinas-desktop-frame-evidence.service",
)


class DesktopGateError(RuntimeError):
    """The guest did not reach a validated graphical desktop."""


def build_qemu_argv(
    *,
    qemu: str,
    kernel: Path,
    rootfs: Path,
    stage1: Path,
    dtb: Path,
    log_dir: Path,
    memory: str,
    smp: int,
) -> list[str]:
    root_drive = f"if=none,format=raw,id=root,file={rootfs},snapshot=on"
    monitor = log_dir / "qemu-monitor.sock"
    return [
        qemu,
        "-machine",
        "virt",
        "-cpu",
        "rv64,svpbmt=true,zkr=true",
        "-m",
        memory,
        "-smp",
        str(smp),
        "-display",
        "none",
        "-device",
        "virtio-keyboard-device",
        "-device",
        "virtio-tablet-device",
        "-serial",
        "stdio",
        "-monitor",
        f"unix:{monitor},server,nowait",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-initrd",
        str(stage1),
        "-dtb",
        str(dtb),
        "-append",
        "console=ttyS0 loglevel=4 "
        "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1 "
        "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct "
        "init=/init -- --root-init=systemd",
        "-drive",
        root_drive,
        "-device",
        "virtio-blk-device,drive=root",
    ]


def _prepare_log_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.lstat().st_mode & 0o077:
        raise DesktopGateError("log directory must be mode 0700")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _hmp_pmemsave(monitor: Path, address: int, size: int, output: Path) -> None:
    """Ask the QEMU monitor for a physical-memory dump."""
    if '"' in str(output):
        raise DesktopGateError("framebuffer output path contains a quote")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(monitor))
    except OSError as error:
        raise DesktopGateError(f"cannot connect to QEMU monitor: {error}") from error
    deadline = time.monotonic() + 15.0
    with sock:
        pending = b""
        while b"(qemu)" not in pending:
            if time.monotonic() >= deadline:
                raise DesktopGateError("QEMU monitor did not publish its prompt")
            try:
                pending += sock.recv(65536)
            except socket.timeout:
                continue
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        sock.sendall(f'pmemsave 0x{address:x} {size} "{output}"\r'.encode())
        reply = b""
        while b"(qemu)" not in reply:
            if time.monotonic() >= deadline:
                raise DesktopGateError("QEMU monitor did not finish pmemsave")
            try:
                reply += sock.recv(65536)
            except socket.timeout:
                continue
    text = reply.decode("utf-8", "replace")
    if "invalid char" in text or "Try \"help" in text:
        raise DesktopGateError(f"QEMU monitor rejected pmemsave: {text.strip()}")
    if not output.is_file():
        raise DesktopGateError("QEMU monitor did not create the framebuffer dump")


def _read_framebuffer(
    raw: Path,
    *,
    width: int,
    height: int,
    stride: int,
    min_nonzero: float,
    min_colors: int,
) -> dict[str, object]:
    expected = stride * height
    data = raw.read_bytes()
    if len(data) != expected:
        raise DesktopGateError(
            f"framebuffer is {len(data)} bytes, expected {expected}"
        )
    if stride < width * 4:
        raise DesktopGateError("framebuffer stride is smaller than one scanline")
    total = width * height
    nonzero = 0
    colors: set[int] = set()
    for y in range(height):
        row = data[y * stride : y * stride + width * 4]
        for word in memoryview(row).cast("I"):
            value = word & 0xFFFFFF
            colors.add(value)
            if value:
                nonzero += 1
    fraction = nonzero / total
    if fraction < min_nonzero:
        raise DesktopGateError(
            f"framebuffer is blank: {fraction:.4f} non-black pixels"
        )
    if len(colors) < min_colors:
        raise DesktopGateError(
            f"framebuffer has too few colors: {len(colors)} < {min_colors}"
        )
    return {
        "width": width,
        "height": height,
        "stride": stride,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "nonzero_pixels": nonzero,
        "nonzero_fraction": round(fraction, 6),
        "distinct_colors": len(colors),
    }


def _write_ppm(raw: Path, output: Path, *, width: int, height: int, stride: int) -> None:
    data = raw.read_bytes()
    pixels = bytearray(width * height * 3)
    for y in range(height):
        source = y * stride
        target = y * width * 3
        row = data[source : source + width * 4]
        pixels[target : target + width * 3 : 3] = row[2::4]
        pixels[target + 1 : target + width * 3 : 3] = row[1::4]
        pixels[target + 2 : target + width * 3 : 3] = row[0::4]
    output.write_bytes(f"P6\n{width} {height}\n255\n".encode() + bytes(pixels))


def _expect_evidence(
    serial: Serial,
    pattern: re.Pattern[str],
    *,
    deadline: float,
) -> str:
    while True:
        match = pattern.search(serial.buffer)
        if match is not None:
            matched = serial.buffer[: match.end()]
            serial.buffer = serial.buffer[match.end() :]
            return matched
        for failure in EVIDENCE_FAILURES:
            if failure in serial.buffer:
                raise DesktopGateError(
                    f"guest evidence failed: {failure}\n"
                    f"--- serial tail ---\n{serial.buffer[-4000:]}"
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DesktopGateError(
                f"timeout waiting for {pattern.pattern!r}\n"
                f"--- serial tail ---\n{serial.buffer[-4000:]}"
            )
        serial._read(min(remaining, 1.0))


def _changed_fraction(before: bytes, after: bytes) -> float:
    if len(before) != len(after):
        raise DesktopGateError("framebuffer dumps have different sizes")
    changed = sum(left != right for left, right in zip(before, after))
    return changed / len(before)


def _capture_framebuffer(
    monitor: Path,
    args: argparse.Namespace,
    serial: Serial,
    raw_path: Path,
    ppm_path: Path,
    *,
    attempts: int = 10,
) -> tuple[dict[str, object], bytes]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        _hmp_pmemsave(
            monitor,
            args.fb_address,
            args.fb_stride * args.fb_height,
            raw_path,
        )
        try:
            info = _read_framebuffer(
                raw_path,
                width=args.fb_width,
                height=args.fb_height,
                stride=args.fb_stride,
                min_nonzero=args.fb_min_nonzero,
                min_colors=args.fb_min_colors,
            )
        except DesktopGateError as error:
            last_error = error
            if attempt + 1 < attempts:
                # Keep reading the serial pipe while the desktop finishes
                # painting; otherwise a chatty guest can block QEMU on a full
                # pipe during the bounded capture retries.
                serial._read(0.0)
                time.sleep(5)
            continue
        _write_ppm(
            raw_path,
            ppm_path,
            width=args.fb_width,
            height=args.fb_height,
            stride=args.fb_stride,
        )
        return info, raw_path.read_bytes()
    assert last_error is not None
    raise last_error


def _capture_until_changed(
    monitor: Path,
    args: argparse.Namespace,
    serial: Serial,
    desktop_raw: bytes,
    raw_path: Path,
    ppm_path: Path,
    *,
    timeout: float,
) -> tuple[dict[str, object], bytes, float]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    last_change = 0.0
    while time.monotonic() < deadline:
        _hmp_pmemsave(
            monitor,
            args.fb_address,
            args.fb_stride * args.fb_height,
            raw_path,
        )
        try:
            info = _read_framebuffer(
                raw_path,
                width=args.fb_width,
                height=args.fb_height,
                stride=args.fb_stride,
                min_nonzero=args.fb_min_nonzero,
                min_colors=args.fb_min_colors,
            )
        except DesktopGateError as error:
            last_error = error
            serial._read(0.0)
            time.sleep(5)
            continue
        data = raw_path.read_bytes()
        last_change = _changed_fraction(desktop_raw, data)
        if last_change >= args.fb_min_change:
            _write_ppm(
                raw_path,
                ppm_path,
                width=args.fb_width,
                height=args.fb_height,
                stride=args.fb_stride,
            )
            return info, data, last_change
        serial._read(0.0)
        time.sleep(5)
    detail = f"last change {last_change:.6f}"
    if last_error is not None:
        detail += f", last error: {last_error}"
    raise DesktopGateError(f"Firefox window did not paint: {detail}")


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    _prepare_log_dir(args.log_dir)
    monitor = args.log_dir / "qemu-monitor.sock"
    try:
        monitor.unlink()
    except FileNotFoundError:
        pass
    serial_log = args.log_dir / "desktop-serial.log"
    desktop_raw_path = args.log_dir / "desktop-framebuffer.raw"
    desktop_ppm_path = args.log_dir / "desktop-framebuffer.ppm"
    raw_path = args.log_dir / "framebuffer.raw"
    ppm_path = args.log_dir / "framebuffer.ppm"
    argv = build_qemu_argv(
        qemu=shutil.which(args.qemu) or args.qemu,
        kernel=args.kernel,
        rootfs=args.rootfs,
        stage1=args.stage1,
        dtb=args.dtb,
        log_dir=args.log_dir,
        memory=args.memory,
        smp=args.smp,
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    serial = Serial(process, serial_log)
    reached: list[str] = []
    try:
        for marker in DESKTOP_MARKERS:
            serial.expect(re.compile(re.escape(marker)), args.timeout)
            reached.append(marker)
        deadline = time.monotonic() + args.firefox_timeout
        _expect_evidence(serial, DESKTOP_FRAME, deadline=deadline)
        desktop_framebuffer, desktop_raw = _capture_framebuffer(
            monitor, args, serial, desktop_raw_path, desktop_ppm_path
        )
        window_match = _expect_evidence(serial, FIREFOX_WINDOW, deadline=deadline)
        _expect_evidence(serial, FRAME_READY, deadline=deadline)
        window_values = FIREFOX_WINDOW.search(window_match)
        if window_values is None:
            raise DesktopGateError("guest Firefox marker is malformed")

        framebuffer, _, changed = _capture_until_changed(
            monitor,
            args,
            serial,
            desktop_raw,
            raw_path,
            ppm_path,
            timeout=args.firefox_paint_timeout,
        )
        return {
            "status": "pass",
            "kernel": str(args.kernel),
            "rootfs": str(args.rootfs),
            "stage1": str(args.stage1),
            "dtb": str(args.dtb),
            "markers": reached,
            "firefox": {
                "window_id": window_values.group(1),
                "title": window_values.group(2),
            },
            "desktop_framebuffer": desktop_framebuffer,
            "framebuffer": framebuffer,
            "changed_fraction": round(changed, 6),
            "evidence": {
                "desktop_raw": str(desktop_raw_path),
                "desktop_ppm": str(desktop_ppm_path),
                "raw": str(raw_path),
                "ppm": str(ppm_path),
                "serial": str(serial_log),
            },
            "log": str(serial_log),
        }
    except Exception as error:
        raise DesktopGateError(
            f"{error}\n--- serial tail ---\n{serial.buffer[-4000:]}"
        ) from error
    finally:
        serial.close()
        _stop(process)
        try:
            monitor.unlink()
        except FileNotFoundError:
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--stage1", type=Path, required=True)
    parser.add_argument("--dtb", type=Path, required=True)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("target/riscv-debian-desktop"),
    )
    parser.add_argument("--qemu", default="qemu-system-riscv64")
    parser.add_argument("--memory", default="2G")
    parser.add_argument("--smp", type=int, default=4, choices=(4,))
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--firefox-timeout", type=float, default=600.0)
    parser.add_argument("--firefox-paint-timeout", type=float, default=120.0)
    parser.add_argument("--fb-address", type=lambda value: int(value, 0), default=0xF0000000)
    parser.add_argument("--fb-width", type=int, default=1280)
    parser.add_argument("--fb-height", type=int, default=1024)
    parser.add_argument("--fb-stride", type=int, default=5120)
    parser.add_argument("--fb-min-nonzero", type=float, default=0.05)
    parser.add_argument("--fb-min-colors", type=int, default=64)
    parser.add_argument("--fb-min-change", type=float, default=0.005)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for name in ("kernel", "rootfs", "stage1", "dtb"):
        path = getattr(args, name)
        if not path.is_file():
            print(f"{name} not found: {path}", file=sys.stderr)
            return 2
    if shutil.which(args.qemu) is None:
        print(f"QEMU executable not found: {args.qemu}", file=sys.stderr)
        return 2
    try:
        result = run_gate(args)
    except DesktopGateError as error:
        result = {"status": "fail", "error": str(error)}
        (args.log_dir / "result.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, indent=2))
        return 1
    (args.log_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
