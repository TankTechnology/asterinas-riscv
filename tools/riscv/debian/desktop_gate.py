#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the browser-web image directly and verify the graphical desktop.

This is the compact replacement for the historical U-Boot/M3/M4 gate stack:
the kernel loads Stage1 as an initrd, Stage1 mounts the browser root, and QMP
captures the 1280x1024 scanout after the desktop session reaches its panel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
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
        "-vga",
        "none",
        "-device",
        "bochs-display,id=desktop-display,xres=1280,yres=1024",
        "-device",
        "virtio-keyboard-device",
        "-device",
        "virtio-tablet-device",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-initrd",
        str(stage1),
        "-dtb",
        str(dtb),
        "-append",
        "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd",
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


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    _prepare_log_dir(args.log_dir)
    serial_log = args.log_dir / "desktop-serial.log"
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
        return {
            "status": "pass",
            "kernel": str(args.kernel),
            "rootfs": str(args.rootfs),
            "stage1": str(args.stage1),
            "dtb": str(args.dtb),
            "markers": reached,
            "log": str(serial_log),
        }
    except Exception as error:
        raise DesktopGateError(
            f"{error}\n--- serial tail ---\n{serial.buffer[-4000:]}"
        ) from error
    finally:
        serial.close()
        _stop(process)


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
