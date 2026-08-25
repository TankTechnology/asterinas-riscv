#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the signed Debian desktop profile and capture framebuffer evidence."""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from tools.riscv.debian.rootfs.contract import load_manifest
from tools.riscv.debian.rootfs.gate_protocol import GateResult, qemu_argv
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    parse_gate_args,
)
from tools.riscv.debian.rootfs.rootfs_gate_backend import (
    ConcreteOperations,
    _safe_output,
)
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


DESKTOP_M3_BOOTARGS = "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd"
DESKTOP_M3_MILESTONES = (
    "DEBIAN_DESKTOP_M3_UDEV state=active",
    "DEBIAN_DESKTOP_M3_LOGIND state=active",
    "DEBIAN_DESKTOP_M3_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M3_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_M3_XORG framebuffer=fbdev display=:0",
    "DEBIAN_DESKTOP_M3_CLIENTS window-manager=matchbox terminal=xterm",
    "DEBIAN_DESKTOP_M3_READY user=asterinas display=:0",
)
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_ANSI_ESCAPE_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_BOCHS_BAR_RE = re.compile(rb"^\s*0\s+(0x[0-9a-fA-F]+)\s+", re.MULTILINE)
_FATAL_MARKERS = (
    (b"kernel panic", "kernel panic"),
    (b"debian_desktop_m3_fail reason=", "desktop guest failure"),
    (b"(ee) no screens found", "Xorg has no screens"),
    (b"fatal server error", "Xorg fatal server error"),
    (b"segmentation fault", "userspace segmentation fault"),
)


def desktop_m3_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the frozen headless-display QEMU contract for Desktop M3."""

    arguments.setdefault("smp", 4)
    arguments.setdefault("dtb_enabled_cpu_count", 4)
    arguments["graphical"] = True
    return qemu_argv(**arguments)


def classify_desktop_m3(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Classify fully drained serial evidence without trusting a screenshot alone."""

    if not expected_debian_release:
        return GateResult(False, "missing expected Debian release", None)
    clean = _ANSI_ESCAPE_RE.sub(b"", transcript).lower()
    for marker, reason in _FATAL_MARKERS:
        if marker in clean:
            return GateResult(False, reason, None)

    original = _ANSI_ESCAPE_RE.sub(b"", transcript)
    positions = [original.find(marker.encode()) for marker in DESKTOP_M3_MILESTONES]
    missing = next(
        (
            marker
            for marker, position in zip(DESKTOP_M3_MILESTONES, positions)
            if position < 0
        ),
        None,
    )
    if missing is not None:
        return GateResult(False, f"missing desktop milestone: {missing}", None)
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return GateResult(False, "desktop milestones out of order", None)
    return GateResult(True, "pass", None)


def _ppm_token(contents: bytes, offset: int) -> tuple[bytes, int]:
    while offset < len(contents):
        if contents[offset : offset + 1] == b"#":
            newline = contents.find(b"\n", offset)
            if newline < 0:
                raise GateFailure("invalid PPM comment")
            offset = newline + 1
        elif contents[offset : offset + 1].isspace():
            offset += 1
        else:
            break
    end = offset
    while end < len(contents) and not contents[end : end + 1].isspace():
        end += 1
    if end == offset:
        raise GateFailure("invalid PPM header")
    return contents[offset:end], end


def inspect_ppm(
    contents: bytes, *, expected_width: int = 1280, expected_height: int = 1024
) -> dict[str, int]:
    """Validate a bounded QEMU P6 screendump and reject a blank framebuffer."""

    if len(contents) > MAX_SCREENSHOT_BYTES:
        raise GateFailure("PPM screenshot exceeds byte cap")
    offset = 0
    tokens = []
    for _ in range(4):
        token, offset = _ppm_token(contents, offset)
        tokens.append(token)
    if tokens[0] != b"P6":
        raise GateFailure("screenshot is not a binary PPM")
    try:
        width, height, maximum = map(int, tokens[1:])
    except ValueError as error:
        raise GateFailure("invalid PPM dimensions") from error
    if (width, height) != (expected_width, expected_height) or maximum != 255:
        raise GateFailure("unexpected PPM geometry")
    if offset >= len(contents) or not contents[offset : offset + 1].isspace():
        raise GateFailure("invalid PPM pixel separator")
    pixels = contents[offset + 1 :]
    expected_size = width * height * 3
    if len(pixels) != expected_size:
        raise GateFailure("PPM pixel payload has the wrong size")
    first = pixels[:3]
    distinct = {first}
    non_background_pixels = 0
    for index in range(3, len(pixels), 3):
        pixel = pixels[index : index + 3]
        if pixel != first:
            non_background_pixels += 1
        if len(distinct) < 256:
            distinct.add(pixel)
    if len(distinct) < 2:
        raise GateFailure("PPM framebuffer contains a single color")
    if non_background_pixels < max(1, width * height // 100):
        raise GateFailure("PPM framebuffer has insufficient rendered content")
    return {
        "width": width,
        "height": height,
        "pixel_count": width * height,
        "distinct_sampled_colors": len(distinct),
        "non_background_pixels": non_background_pixels,
    }


def capture_rendered_ppm(
    monitor: Any,
    screenshot: Path,
    deadline: float,
    *,
    expected_width: int = 1280,
    expected_height: int = 1024,
    retry_interval: float = 1.0,
) -> tuple[bytes, dict[str, int]]:
    """Capture until the first non-blank frame, within one total deadline."""

    quoted = str(screenshot).replace("\\", "\\\\").replace('"', '\\"')
    while True:
        if time.monotonic() >= deadline:
            raise GateFailure("PPM framebuffer contains a single color")
        monitor.command(f'screendump "{quoted}"', deadline)
        contents = screenshot.read_bytes()
        try:
            metadata = inspect_ppm(
                contents,
                expected_width=expected_width,
                expected_height=expected_height,
            )
            return contents, metadata
        except GateFailure as error:
            if error.reason not in (
                "PPM framebuffer contains a single color",
                "PPM framebuffer has insufficient rendered content",
            ):
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateFailure("PPM framebuffer contains a single color")
        time.sleep(min(retry_interval, remaining))


class DesktopM3Operations(ConcreteOperations):
    """Graphical adapter around the existing pinned rootfs gate backend."""

    def __init__(self, config: GateConfig) -> None:
        super().__init__(config)
        self._screenshot = b""
        self._screenshot_metadata: dict[str, int] = {}

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_m3_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.ext2",
            "desktop-m3.serial.log",
            "desktop-m3.ppm",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(super().validate_inputs(config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        if manifest.schema_version != 3 or manifest.profile != "desktop-m3":
            raise GateFailure("rootfs manifest is not the desktop-m3 profile")
        identity["profile"] = manifest.profile
        return identity

    def launch(self, config: GateConfig, prepared: Any) -> dict[str, Any]:
        return super().launch(config, prepared, 1)

    def _boot_commands(self, framebuffer_address: int) -> tuple[str, ...]:
        node = f"framebuffer@{framebuffer_address:x}"
        return (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
            "fdt addr 0x90000000",
            "fdt resize 0x1000",
            f"fdt mknode / {node}",
            f'fdt set /{node} compatible "simple-framebuffer"',
            f"fdt set /{node} reg <0x0 {framebuffer_address:#x} 0x0 0x1000000>",
            f"fdt set /{node} width <0x500>",
            f"fdt set /{node} height <0x400>",
            f"fdt set /{node} stride <0x1400>",
            f'fdt set /{node} format "x8r8g8b8"',
            f'fdt set /{node} status "okay"',
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{DESKTOP_M3_BOOTARGS}"',
        )

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        serial = session["serial"]
        deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(b"=> ", deadline)
        self._send_uboot(session, "pci enum", 1, deadline)
        bar_start = serial.checkpoint()
        self._send_uboot(session, "pci bar 0.1.0", 2, deadline)
        match = _BOCHS_BAR_RE.search(serial.transcript[bar_start:])
        if match is None:
            raise GateFailure("failed to discover bochs framebuffer BAR0")
        framebuffer_address = int(match.group(1), 16)
        for index, command in enumerate(self._boot_commands(framebuffer_address), 3):
            self._send_uboot(session, command, index, deadline)

        marker = f"__ASTERINAS_DESKTOP_BOOT_{secrets.token_hex(8).upper()}__"
        split = len(marker) // 2
        serial.send(
            (
                f"setenv ast_ba {marker[:split]}; setenv ast_bb {marker[split:]}; "
                "echo ${ast_ba}${ast_bb}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x90000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)
        completion = serial.wait_for_any(
            (
                DESKTOP_M3_MILESTONES[-1].encode(),
                b"DEBIAN_DESKTOP_M3_FAIL reason=",
            ),
            time.monotonic() + config.boot_timeout,
        )
        if completion.startswith(b"DEBIAN_DESKTOP_M3_FAIL"):
            raise GateFailure("guest reported desktop failure")

        screenshot = session["directory"] / "desktop-m3.ppm"
        self._screenshot, self._screenshot_metadata = capture_rendered_ppm(
            session["monitor"],
            screenshot,
            time.monotonic() + config.command_timeout,
        )

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        del prepared
        self._require_config(config)
        output = self._require_output()
        result["qemu_argv"] = self._attempted_argv
        result["screenshot"] = self._screenshot_metadata
        output.atomic_write("desktop-m3.serial.log", transcript)
        if self._screenshot:
            output.atomic_write("desktop-m3.ppm", self._screenshot, mode=0o600)
        document = json.dumps(result, indent=2, sort_keys=True) + "\n"
        output.atomic_write("result.json", document.encode())


def orchestrate_desktop_m3_gate(
    config: GateConfig, operations: DesktopM3Operations
) -> dict[str, object]:
    """Reuse the bounded one-process lifecycle with the desktop classifier."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m3,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM3Operations(config) as operations:
            result = orchestrate_desktop_m3_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m3-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m3-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
