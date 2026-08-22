"""Strict, bounded validation of QEMU PPM screendumps."""

from __future__ import annotations

from dataclasses import dataclass
import re


_HEADER = re.compile(rb"\AP6\n([1-9][0-9]*) ([1-9][0-9]*)\n255\n")


@dataclass(frozen=True)
class PpmAudit:
    width: int
    height: int
    max_value: int
    non_black_pixels: int
    distinct_colors_lower_bound: int
    bounding_box: tuple[int, int, int, int] | None
    passed: bool


def audit_ppm(payload: bytes, *, expected_width: int, expected_height: int) -> PpmAudit:
    """Audit one exact P6/255 image without retaining its pixel colors."""
    if expected_width <= 0 or expected_height <= 0:
        raise ValueError("expected dimensions must be positive")

    match = _HEADER.match(payload)
    if match is None:
        raise ValueError("PPM header is not strict P6")
    width, height = (int(value) for value in match.groups())
    if (width, height) != (expected_width, expected_height):
        raise ValueError("PPM dimensions differ from the registered display")

    pixel_data = memoryview(payload)[match.end() :]
    expected_size = width * height * 3
    if len(pixel_data) != expected_size:
        raise ValueError("PPM pixel data has an invalid length")

    colors: set[bytes] = set()
    non_black = 0
    min_x = min_y = width
    max_x = max_y = -1
    for offset in range(0, expected_size, 3):
        color = bytes(pixel_data[offset : offset + 3])
        if len(colors) < 3:
            colors.add(color)
        if color == b"\0\0\0":
            continue
        non_black += 1
        pixel_index = offset // 3
        x, y = pixel_index % width, pixel_index // width
        min_x, min_y = min(min_x, x), min(min_y, y)
        max_x, max_y = max(max_x, x), max(max_y, y)

    bounding_box = None if non_black == 0 else (min_x, min_y, max_x, max_y)
    distinct_colors = len(colors)
    return PpmAudit(
        width=width,
        height=height,
        max_value=255,
        non_black_pixels=non_black,
        distinct_colors_lower_bound=distinct_colors,
        bounding_box=bounding_box,
        passed=non_black >= 64 and distinct_colors >= 3 and bounding_box is not None,
    )
