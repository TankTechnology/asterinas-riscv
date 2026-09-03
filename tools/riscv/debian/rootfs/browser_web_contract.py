#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Shared Firefox/Baidu acceptance markers and screenshot validation."""

from __future__ import annotations

import hashlib
import struct
from typing import Mapping
import zlib

from tools.riscv.debian.rootfs.desktop_m5_network_gate import NetworkMode
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure
from tools.riscv.megrez_network_fixture import BROWSER_PNG_CAPTURE_PATH


MAX_WEB_SCREENSHOT_PIXELS_BYTES = 64 * 1024 * 1024


def firefox_ready_marker(mode: NetworkMode) -> str:
    """Return the single mode-qualified physical/QEMU Firefox marker."""

    if not isinstance(mode, NetworkMode):
        raise ValueError("Firefox ready mode must be a NetworkMode")
    return (
        f"DEBIAN_FIREFOX_BAIDU_READY mode={mode.value} home=pass logo=pass "
        "search=pass input=pass stable=pass screenshot=baidu-search.png"
    )


def validate_png_evidence(contents: bytes, name: str) -> None:
    """Validate one bounded, non-interlaced eight-bit PNG evidence file."""

    if not contents.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GateFailure(f"browser web screenshot is not PNG: {name}")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(contents):
        if offset + 12 > len(contents):
            raise GateFailure(f"browser web screenshot is truncated: {name}")
        length = struct.unpack(">I", contents[offset : offset + 4])[0]
        kind = contents[offset + 4 : offset + 8]
        end = offset + 12 + length
        if length > MAX_WEB_SCREENSHOT_PIXELS_BYTES or end > len(contents):
            raise GateFailure(f"browser web screenshot chunk is invalid: {name}")
        payload = contents[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", contents[offset + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise GateFailure(f"browser web screenshot CRC is invalid: {name}")
        chunks.append((kind, payload))
        offset = end
        if kind == b"IEND":
            break
    if offset != len(contents) or not chunks or chunks[0][0] != b"IHDR":
        raise GateFailure(f"browser web screenshot structure is invalid: {name}")
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise GateFailure(f"browser web screenshot IHDR is invalid: {name}")
    width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if not width or not height or width > 16384 or height > 16384:
        raise GateFailure(f"browser web screenshot dimensions are invalid: {name}")
    if channels is None or depth != 8 or compression or filtering or interlace:
        raise GateFailure(f"browser web screenshot format is unsupported: {name}")
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    expected_size = height * (1 + width * channels)
    if expected_size > MAX_WEB_SCREENSHOT_PIXELS_BYTES:
        raise GateFailure(f"browser web screenshot decoded size is excessive: {name}")
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(compressed, expected_size + 1)
    except zlib.error as error:
        raise GateFailure(f"browser web screenshot pixels are invalid: {name}") from error
    if (
        len(pixels) != expected_size
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not decompressor.eof
    ):
        raise GateFailure(f"browser web screenshot pixel size is invalid: {name}")
    if any(pixels[row * (1 + width * channels)] > 4 for row in range(height)):
        raise GateFailure(f"browser web screenshot filter is invalid: {name}")


def validate_uploaded_baidu_screenshot(
    summary: Mapping[str, object] | None,
    payload: bytes | None,
    *,
    expected_payload: bytes | None = None,
) -> dict[str, object]:
    """Bind one fixture upload to a valid Baidu PNG and immutable digest."""

    if (
        summary is None
        or payload is None
        or summary.get("path") != BROWSER_PNG_CAPTURE_PATH
        or summary.get("bytes") != len(payload)
        or summary.get("sha256") != hashlib.sha256(payload).hexdigest()
        or (expected_payload is not None and payload != expected_payload)
    ):
        raise GateFailure("Firefox screenshot upload evidence mismatch")
    validate_png_evidence(payload, "baidu-search.png")
    return dict(summary)
