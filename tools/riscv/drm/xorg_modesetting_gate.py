#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate a captured Xorg modesetting session and its QMP screenshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

try:
    from qemu_scanout_contract import ScanoutContractError, parse_ppm
except ModuleNotFoundError:  # Package import from tools.riscv.tests.
    from tools.riscv.qemu_scanout_contract import ScanoutContractError, parse_ppm


EXPECTED_WIDTH = 1280
EXPECTED_HEIGHT = 800
DESKTOP_MARKERS = (
    b"DEBIAN_DESKTOP_DRM_UDEV state=active",
    b"DEBIAN_DESKTOP_DRM_LOGIND state=active",
    b"DEBIAN_DESKTOP_DRM_SESSION user=asterinas tty=tty1",
    b"DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev",
    b"DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio_gpu drm=active display=:0",
    b"DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm",
    b"DEBIAN_DESKTOP_DRM_READY user=asterinas display=:0",
)
FAILURE_MARKER = b"DEBIAN_DESKTOP_DRM_FAIL reason="
DRI3_CARD_MARKER = b"DRI3_OPEN node=/dev/dri/card0"
GL_MARKER = re.compile(rb"DEBIAN_DESKTOP_DRM_GL_XORG accel=([A-Za-z0-9_-]+)")
MAX_INPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class XorgModesettingResult:
    passed: bool
    reason: str
    marker_count: int
    width: int | None = None
    height: int | None = None
    pixel_sha256: str | None = None
    ppm_sha256: str | None = None
    distinct_colors: int = 0
    non_background_pixels: int = 0
    renderer: str | None = None


def _read_bounded(path: Path, label: str) -> bytes:
    payload = path.read_bytes()
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} exceeds the byte cap")
    return payload


def validate_xorg_modesetting(
    serial: bytes,
    screenshot: bytes,
    *,
    expected_width: int = EXPECTED_WIDTH,
    expected_height: int = EXPECTED_HEIGHT,
) -> XorgModesettingResult:
    """Require ordered Xorg/DRM markers and non-blank screenshot pixels."""

    if len(serial) > MAX_INPUT_BYTES or len(screenshot) > MAX_INPUT_BYTES:
        raise ValueError("Xorg DRM evidence exceeds the byte cap")
    if FAILURE_MARKER in serial:
        return XorgModesettingResult(False, "guest reported DRM desktop failure", 0)

    offset = 0
    for marker in DESKTOP_MARKERS:
        position = serial.find(marker, offset)
        if position < 0:
            return XorgModesettingResult(False, f"missing or unordered marker: {marker.decode()}", 0)
        offset = position + len(marker)
    if serial.count(DESKTOP_MARKERS[4]) != 1 or b"framebuffer=fbdev" in serial:
        return XorgModesettingResult(False, "Xorg provider is not an unambiguous modesetting session", len(DESKTOP_MARKERS))
    if DRI3_CARD_MARKER not in serial:
        return XorgModesettingResult(False, "Xorg did not open /dev/dri/card0", len(DESKTOP_MARKERS))

    try:
        image = parse_ppm(
            screenshot,
            expected_width=expected_width,
            expected_height=expected_height,
        )
    except ScanoutContractError as error:
        return XorgModesettingResult(False, f"screenshot: {error}", len(DESKTOP_MARKERS))

    first = image.pixels[:3]
    non_background = sum(
        image.pixels[offset : offset + 3] != first
        for offset in range(0, len(image.pixels), 3)
    )
    distinct = len(image.colors)
    ppm_sha256 = hashlib.sha256(screenshot).hexdigest()
    renderer_match = GL_MARKER.search(serial)
    renderer = renderer_match.group(1).decode() if renderer_match else None
    if renderer != "virgl":
        return XorgModesettingResult(
            False,
            f"Xorg renderer is {renderer or 'missing'}; expected virgl",
            len(DESKTOP_MARKERS),
            image.width,
            image.height,
            image.pixel_sha256,
            ppm_sha256,
            distinct,
            non_background,
            renderer,
        )
    if distinct < 8:
        return XorgModesettingResult(
            False,
            "screenshot has too few colors",
            len(DESKTOP_MARKERS),
            image.width,
            image.height,
            image.pixel_sha256,
            ppm_sha256,
            distinct,
            non_background,
            renderer,
        )
    if non_background < image.width * image.height // 20:
        return XorgModesettingResult(
            False,
            "screenshot has insufficient painted content",
            len(DESKTOP_MARKERS),
            image.width,
            image.height,
            image.pixel_sha256,
            ppm_sha256,
            distinct,
            non_background,
            renderer,
        )
    return XorgModesettingResult(
        True,
        "passed",
        len(DESKTOP_MARKERS),
        image.width,
        image.height,
        image.pixel_sha256,
        ppm_sha256,
        distinct,
        non_background,
        renderer,
    )


def run_validation(serial_path: Path, screenshot_path: Path, output: Path) -> XorgModesettingResult:
    result = validate_xorg_modesetting(
        _read_bounded(serial_path, "Xorg serial log"),
        _read_bounded(screenshot_path, "Xorg screenshot"),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n")
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", required=True, type=Path)
    parser.add_argument("--screenshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(arguments)
    result = run_validation(args.serial_log, args.screenshot, args.output)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
