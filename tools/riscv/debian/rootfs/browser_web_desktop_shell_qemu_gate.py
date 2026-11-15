#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture a bounded Firefox desktop-shell interaction in graphical QEMU."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

if __package__ in (None, ""):
    repository_root = Path(__file__).resolve().parents[4]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

from tools.riscv.debian.rootfs.browser_web_qemu_gate import BrowserWebQemuOperations
from tools.riscv.debian.rootfs.desktop_m3_gate import _BOCHS_BAR_RE
from tools.riscv.debian.rootfs.desktop_m5_network_gate import NetworkMode
from tools.riscv.debian.rootfs.firefox_startup_profile import (
    _config,
    _profile_boot_commands,
    _wait_for_marker_line,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.qemu_ppm import audit_ppm
from tools.riscv.qemu_qmp import ABSOLUTE_AXIS_MAX, capture_screendump, click_tablet
from tools.riscv.qemu_uboot_devices import BOCHS_XRGB8888


class DesktopShellQemuOperations(BrowserWebQemuOperations):
    """Give the frozen browser-web boot a private QMP input/capture socket."""

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        monitor_socket = arguments["monitor_socket"]
        qmp_socket = monitor_socket.with_name("desktop-shell-qmp.sock")
        return (
            *BrowserWebQemuOperations._qemu_argv(**arguments),
            "-qmp",
            f"unix:{qmp_socket},server=on,wait=off",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "kernel",
        "uboot",
        "dtb",
        "stage1-initramfs",
        "root-image",
        "root-manifest",
        "packages-lock",
        "package-checksums",
        "output-directory",
    ):
        parser.add_argument(f"--{option}", required=True, type=Path)
    parser.add_argument("--boot-timeout", type=float, default=360.0)
    parser.add_argument("--smp", type=int, choices=(4,), default=4)
    return parser


def _pixel_to_axis(pixel: int, extent: int) -> int:
    if not 0 <= pixel < extent:
        raise ValueError("desktop input pixel is outside the display")
    return round(pixel * ABSOLUTE_AXIS_MAX / (extent - 1))


def _capture(
    operations: DesktopShellQemuOperations, session: dict[str, Any], name: str
) -> str:
    runtime_directory = session["directory"]
    qmp_socket = runtime_directory / "desktop-shell-qmp.sock"
    ppm = capture_screendump(
        qmp_socket,
        runtime_directory / f"{name}.ppm",
        capture_root=runtime_directory,
        timeout=10.0,
    )
    audit = audit_ppm(
        ppm,
        expected_width=BOCHS_XRGB8888.width,
        expected_height=BOCHS_XRGB8888.height,
    )
    if not audit.passed:
        raise GateFailure(f"{name} screen is blank or invalid")
    operations._require_output().atomic_write(f"desktop-{name}.ppm", ppm)
    return hashlib.sha256(ppm).hexdigest()


def _click_screen(session: dict[str, Any], x: int, y: int) -> None:
    socket = session["directory"] / "desktop-shell-qmp.sock"
    click_tablet(
        socket,
        x=_pixel_to_axis(x, BOCHS_XRGB8888.width),
        y=_pixel_to_axis(y, BOCHS_XRGB8888.height),
        timeout=5.0,
    )


def run(config: GateConfig) -> int:
    _safe_output(config.output_directory)
    operations = DesktopShellQemuOperations(config, network_mode=NetworkMode.DIRECT)
    session = None
    screenshots: dict[str, str] = {}
    markers: list[str] = []
    started = time.monotonic()
    try:
        operations.__enter__()
        operations.invalidate(config)
        snapshots = operations.snapshot_inputs(config)
        identity = operations.validate_inputs(config, snapshots)
        prepared = operations.prepare(config, snapshots, identity)
        session = operations.launch(config, prepared)
        serial = session["serial"]
        deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(b"=> ", deadline)
        operations._send_uboot(session, "pci enum", 1, deadline)
        bar_start = serial.checkpoint()
        operations._send_uboot(session, "pci bar 0.1.0", 2, deadline)
        match = _BOCHS_BAR_RE.search(serial.transcript[bar_start:])
        if match is None:
            raise GateFailure("failed to discover bochs framebuffer BAR0")
        framebuffer_address = int(match.group(1), 16)
        for index, command in enumerate(
            _profile_boot_commands(operations, framebuffer_address), 3
        ):
            operations._send_uboot(session, command, index, deadline)
        boot_marker = "__ASTERINAS_DESKTOP_SHELL_BOOT__"
        serial.send(
            (
                f"echo {boot_marker}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x90000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(boot_marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)
        for marker in (
            b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready",
            b"BROWSER_WEB_DESKTOP_STAGE=desktop-start",
            b"BROWSER_WEB_DESKTOP_STAGE=panel-start",
            b"BOOT_FIRST_WINDOW_READY",
        ):
            line = _wait_for_marker_line(serial, marker, deadline)
            markers.append(line[line.rfind(marker) :].splitlines()[0].decode("ascii", "replace"))
            print(f"DESKTOP_SHELL_MARKER {markers[-1]}", flush=True)

        time.sleep(3)
        screenshots["open"] = _capture(operations, session, "open")
        # Openbox draws the minimize button at the upper-right corner of the
        # fixed 1280x1024 framebuffer. Exercise the real tablet/input path.
        _click_screen(session, 1238, 10)
        time.sleep(2)
        screenshots["minimized"] = _capture(operations, session, "minimized")
        # The menu occupies the leftmost 90 pixels. The three launchbar
        # buttons follow it in Firefox / Files / Terminal order.
        for name, x in (("restored", 105), ("files", 135), ("terminal", 165)):
            _click_screen(session, x, BOCHS_XRGB8888.height - 20)
            time.sleep(3)
            screenshots[name] = _capture(operations, session, name)
        # With all three windows present, switch back to Firefox through the
        # taskbar rather than its launcher. This also proves the panel tracks
        # windows created after boot.
        _click_screen(session, 250, BOCHS_XRGB8888.height - 20)
        time.sleep(2)
        screenshots["switched"] = _capture(operations, session, "switched")
        operations._require_output().atomic_write(
            "desktop-shell-result.json",
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "capture_complete": True,
                        "visual_review_required": True,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "markers": markers,
                        "screenshots_sha256": screenshots,
                        "scope": "qemu-visual-interaction",
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
        )
        print("DESKTOP_SHELL_QEMU_CAPTURED", flush=True)
        return 0
    except BaseException as error:
        print(f"DESKTOP_SHELL_QEMU_ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            try:
                operations._require_output().atomic_write(
                    "desktop-shell.serial.log", session["serial"].transcript
                )
            except BaseException:
                pass
            try:
                session["monitor"].command("quit", time.monotonic() + 5)
            except BaseException:
                pass
            for action in (
                operations.close_monitor,
                lambda current: operations.cleanup_process(current, config),
                lambda current: operations.drain_serial(current, config),
            ):
                try:
                    action(session)
                except BaseException:
                    pass
            try:
                result = subprocess.run(
                    [
                        "debugfs",
                        "-R",
                        "cat /home/asterinas/desktop-m5-session.log",
                        str(config.output_directory / "debian-root.run.ext2"),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0 and len(result.stdout) <= 256 * 1024:
                    operations._require_output().atomic_write(
                        "desktop-shell-session.log", result.stdout
                    )
            except BaseException:
                pass
        try:
            operations._require_output().invalidate("boot.ext4", "debian-root.run.ext2")
        except BaseException:
            pass
        operations.close()


def main() -> int:
    arguments = _parser().parse_args()
    return run(_config(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
