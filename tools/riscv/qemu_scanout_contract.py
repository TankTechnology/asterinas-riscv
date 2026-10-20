#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict host-side contract for a QEMU framebuffer scanout.

The contract is deliberately independent of a particular guest.  QTest proves
that the emulated BAR and VBE registers expose this layout; this module checks
the same layout after a QMP PPM screendump and gives fault-injection tests a
single fail-closed validator.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


class ScanoutContractError(ValueError):
    """A scanout does not satisfy the registered framebuffer contract."""


@dataclass(frozen=True)
class ScanoutMode:
    """The guest-visible mode and its mapped BAR aperture."""

    bar_address: int
    bar_size: int
    width: int
    height: int
    stride: int
    pixel_format: str
    mapped_size: int | None = None

    @property
    def frame_size(self) -> int:
        return self.stride * self.height


@dataclass(frozen=True)
class ScanoutImage:
    width: int
    height: int
    pixel_sha256: str
    colors: frozenset[bytes]
    pixels: bytes


_PPM_HEADER = re.compile(rb"\AP6\n([1-9][0-9]*) ([1-9][0-9]*)\n255\n")


def validate_mode(mode: ScanoutMode, *, expected: ScanoutMode | None = None) -> None:
    """Validate BAR, geometry, stride, format and mapping without side effects."""

    if mode.bar_address <= 0 or mode.bar_address % 0x1000:
        raise ScanoutContractError("scanout BAR is missing or unaligned")
    if mode.bar_size < 4 * 1024 * 1024:
        raise ScanoutContractError("scanout BAR is missing or too small")
    if not 1 <= mode.width <= 16000 or not 1 <= mode.height <= 12000:
        raise ScanoutContractError("scanout dimensions are outside VBE bounds")
    if mode.pixel_format not in {"x8r8g8b8", "BgrReserved"}:
        raise ScanoutContractError("scanout pixel format is unsupported")
    if mode.stride < mode.width * 4 or mode.stride % 4:
        raise ScanoutContractError("scanout stride is invalid")
    if mode.frame_size > mode.bar_size:
        raise ScanoutContractError("scanout frame is truncated by the BAR")
    if mode.mapped_size is not None and mode.frame_size > mode.mapped_size:
        raise ScanoutContractError("scanout mapping is truncated")
    if expected is not None:
        fields = ("bar_address", "bar_size", "width", "height", "stride", "pixel_format")
        for field in fields:
            if getattr(mode, field) != getattr(expected, field):
                raise ScanoutContractError(f"scanout {field} differs from the contract")


def parse_ppm(payload: bytes, *, expected_width: int, expected_height: int) -> ScanoutImage:
    """Parse a bounded QMP P6 screendump and hash exactly its pixel payload."""

    match = _PPM_HEADER.match(payload)
    if match is None:
        raise ScanoutContractError("QMP screendump is not a strict P6 image")
    width, height = (int(value) for value in match.groups())
    if (width, height) != (expected_width, expected_height):
        raise ScanoutContractError("QMP screendump geometry differs from the mode")
    pixels = payload[match.end() :]
    expected_size = width * height * 3
    if len(pixels) != expected_size:
        raise ScanoutContractError("QMP screendump pixel payload is truncated")
    colors = frozenset(pixels[offset : offset + 3] for offset in range(0, len(pixels), 3))
    return ScanoutImage(
        width=width,
        height=height,
        pixel_sha256=hashlib.sha256(pixels).hexdigest(),
        colors=colors,
        pixels=pixels,
    )


def pixel(image: ScanoutImage, x: int, y: int) -> bytes:
    """Return one RGB pixel, rejecting coordinates outside the screenshot."""

    if not 0 <= x < image.width or not 0 <= y < image.height:
        raise ScanoutContractError("pixel coordinate is outside the screendump")
    offset = (y * image.width + x) * 3
    return image.pixels[offset : offset + 3]


def verify_region(image: ScanoutImage, *, x: int, y: int, width: int, height: int, color: bytes) -> None:
    """Require a rectangle to contain exactly one expected RGB color."""

    if len(color) != 3 or width <= 0 or height <= 0:
        raise ScanoutContractError("invalid scanout region")
    if x < 0 or y < 0 or x + width > image.width or y + height > image.height:
        raise ScanoutContractError("scanout region is outside the screendump")
    for row in range(y, y + height):
        for column in range(x, x + width):
            if pixel(image, column, row) != color:
                raise ScanoutContractError("scanout readback differs from the written color")


def validate_fault(fault: str, mode: ScanoutMode) -> str:
    """Run one named negative contract case and return its stable reason."""

    try:
        validate_mode(mode)
    except ScanoutContractError as error:
        return str(error)
    raise ScanoutContractError(f"fault injection {fault} unexpectedly passed")

