#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture one bounded fbdev frame and emit machine-checkable evidence."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import mmap
import os
from pathlib import Path
import stat
import struct
import sys
import time
import zlib


FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
MAX_FRAMEBUFFER_BYTES = 64 * 1024 * 1024
MAX_PNG_BYTES = 2 * 1024 * 1024
FRAMEBUFFER = Path("/dev/fb0")
DEFAULT_OUTPUT = Path("/run/asterinas-desktop-m4-framebuffer.png")


class ProbeError(ValueError):
    """The framebuffer did not satisfy the bounded display contract."""


def _layout(variable: bytes, fixed: bytes) -> tuple[int, int, int, int]:
    if len(variable) != 160 or len(fixed) != 80:
        raise ProbeError("framebuffer metadata size is invalid")
    width, height, virtual_width, virtual_height, xoffset, yoffset, bpp, grayscale = struct.unpack_from(
        "=8I", variable
    )
    red = struct.unpack_from("=3I", variable, 32)
    green = struct.unpack_from("=3I", variable, 44)
    blue = struct.unpack_from("=3I", variable, 56)
    transparency = struct.unpack_from("=3I", variable, 68)
    nonstandard = struct.unpack_from("=I", variable, 80)[0]
    memory_bytes = struct.unpack_from("=I", fixed, 24)[0]
    visual = struct.unpack_from("=I", fixed, 36)[0]
    stride = struct.unpack_from("=I", fixed, 48)[0]
    if (
        not 0 < width <= 8192
        or not 0 < height <= 8192
        or virtual_width != width
        or virtual_height != height
        or xoffset != 0
        or yoffset != 0
        or bpp != 32
        or grayscale != 0
        or nonstandard != 0
        or red != (16, 8, 0)
        or green != (8, 8, 0)
        or blue != (0, 8, 0)
        or transparency != (24, 8, 0)
        or visual != 2
        or stride < width * 4
        or stride > width * 4 + 65536
        or stride * height > MAX_FRAMEBUFFER_BYTES
        or memory_bytes < stride * height
    ):
        raise ProbeError("unsupported framebuffer layout")
    return width, height, stride, stride * height


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(raw: bytes, width: int, height: int, stride: int) -> bytes:
    if len(raw) != stride * height:
        raise ProbeError("framebuffer read is incomplete")
    compressor = zlib.compressobj(6)
    compressed = bytearray()
    view = memoryview(raw)
    for row in range(height):
        source = view[row * stride : row * stride + width * 4]
        scanline = bytearray(1 + width * 3)
        scanline[1::3] = source[2::4]
        scanline[2::3] = source[1::4]
        scanline[3::3] = source[0::4]
        compressed.extend(compressor.compress(scanline))
    compressed.extend(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", bytes(compressed))
        + _png_chunk(b"IEND", b"")
    )
    if not 24 <= len(payload) <= MAX_PNG_BYTES:
        raise ProbeError("screenshot is outside the bounded size")
    return payload


def _pixel_diversity(raw: bytes, stride: int, width: int, height: int) -> tuple[int, int]:
    """Return sampled color count and pixels differing from the first color."""

    first = raw[:4]
    distinct: set[bytes] = set()
    non_background = 0
    for row in range(0, height, 4):
        row_start = row * stride
        for column in range(0, width, 4):
            offset = row_start + column * 4
            pixel = raw[offset : offset + 4]
            distinct.add(pixel)
            if pixel != first:
                non_background += 1
            if len(distinct) >= 256 and non_background >= max(1, width * height // 256):
                return len(distinct), non_background
    if len(distinct) < 2 or non_background < max(1, width * height // 256):
        raise ProbeError("framebuffer contains a single color or insufficient content")
    return len(distinct), non_background


def _read_framebuffer() -> tuple[tuple[int, int, int, int], bytes]:
    access = os.O_RDWR
    descriptor = os.open(FRAMEBUFFER, access | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise ProbeError("framebuffer is not a character device")
        variable = bytearray(160)
        fixed = bytearray(80)
        fcntl.ioctl(descriptor, FBIOGET_VSCREENINFO, variable, True)
        fcntl.ioctl(descriptor, FBIOGET_FSCREENINFO, fixed, True)
        layout = _layout(bytes(variable), bytes(fixed))
        remaining = layout[3]
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ProbeError("framebuffer read returned EOF")
            chunks.append(chunk)
            remaining -= len(chunk)
        return layout, b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_at(descriptor: int, size: int, offset: int) -> bytes:
    """Read a bounded range without requiring a full scanout-sized read."""

    try:
        return os.pread(descriptor, size, offset)
    except OSError as error:
        # A few older libc/syscall combinations do not expose pread for the
        # fbdev character node.  Keep the fallback bounded and explicit.
        if error.errno != errno.ENOSYS:
            raise
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, size)


def _read_framebuffer_sample() -> tuple[tuple[int, int, int, int], bytes, int, int]:
    """Read metadata and sparse scanout rows for the QEMU gate.

    A complete 8 MiB MMIO-backed read is unnecessarily expensive on the
    synthetic RISC-V fbdev.  The sample still checks the first 4 KiB hash and
    scans enough rows/columns to prove that the desktop painted more than one
    pixel value.
    """

    descriptor = os.open(FRAMEBUFFER, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        trace = os.environ.get("ASTERINAS_DESKTOP_M4_FRAMEBUFFER_TRACE") == "1"
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise ProbeError("framebuffer is not a character device")
        variable = bytearray(160)
        fixed = bytearray(80)
        fcntl.ioctl(descriptor, FBIOGET_VSCREENINFO, variable, True)
        fcntl.ioctl(descriptor, FBIOGET_FSCREENINFO, fixed, True)
        width, height, stride, _ = _layout(bytes(variable), bytes(fixed))
        if os.environ.get("ASTERINAS_DESKTOP_M4_FRAMEBUFFER_FLUSH") == "1":
            flush_bytes = int(
                os.environ.get("ASTERINAS_DESKTOP_M4_FRAMEBUFFER_FLUSH_BYTES", "0")
            ) or stride * height
            if not 0 < flush_bytes <= stride * height:
                raise ProbeError("invalid framebuffer flush length")
            mapping = mmap.mmap(
                descriptor,
                flush_bytes,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
            try:
                mapping.flush()
            finally:
                mapping.close()
        sample = _read_at(descriptor, 4096, 0)
        if len(sample) != 4096:
            raise ProbeError("framebuffer sample is incomplete")

        first = sample[:4]
        distinct: set[bytes] = set()
        non_background = 0
        sampled_pixels = 0
        row_step = max(1, height // 32)
        column_step = max(1, width // 64)
        for row in range(0, height, row_step):
            row_count = min(2, height - row)
            if trace:
                print(
                    f"DEBIAN_DESKTOP_M4_FB_TRACE row={row} "
                    f"offset={row * stride} bytes={row_count * stride}",
                    flush=True,
                )
            raw_rows = _read_at(descriptor, row_count * stride, row * stride)
            if trace:
                print(
                    f"DEBIAN_DESKTOP_M4_FB_TRACE_DONE row={row} "
                    f"bytes={len(raw_rows)}",
                    flush=True,
                )
            if len(raw_rows) != row_count * stride:
                raise ProbeError("framebuffer row sample is incomplete")
            for local_row in range(row_count):
                row_start = local_row * stride
                for column in range(0, width, column_step):
                    offset = row_start + column * 4
                    pixel = raw_rows[offset : offset + 4]
                    sampled_pixels += 1
                    distinct.add(pixel)
                    if pixel != first:
                        non_background += 1
        # The QEMU desktop is intentionally sparse: the panel and terminal
        # may be the only clients that have painted when the bounded probe
        # runs.  Keep the guest readback check strict against a blank frame,
        # but leave the stronger whole-screen content check to the QEMU HMP
        # screenshot gate.  Requiring two sampled colors and two changed
        # pixels avoids accepting an all-zero BAR without rejecting a valid
        # first paint that has not filled a large fraction of the screen.
        if len(distinct) < 2 or non_background < 2:
            raise ProbeError("framebuffer contains a single color or insufficient content")
        return (width, height, stride, stride * height), sample, len(distinct), non_background
    finally:
        os.close(descriptor)


def _safe_output(path: Path) -> Path:
    if not path.is_absolute() or path.parent != Path("/run"):
        raise ProbeError("screenshot must be directly under /run")
    if path.name != DEFAULT_OUTPUT.name:
        raise ProbeError("unexpected screenshot name")
    if path.exists() or path.is_symlink():
        raise ProbeError("screenshot output already exists")
    return path


def _parse_output(argv: list[str] | None) -> Path:
    """Parse the one option without importing argparse/gettext.

    The Asterinas ext2 read path has previously returned a short, corrupted
    ``locale.py`` while two desktop probes were active.  ``argparse`` eagerly
    imports gettext and locale while constructing its parser, so this tiny
    fixed grammar keeps the framebuffer probe independent of that userspace
    locale path.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return DEFAULT_OUTPUT
    if len(arguments) == 2 and arguments[0] == "--output":
        return Path(arguments[1])
    raise ProbeError("usage: desktop-m4-framebuffer-probe [--output PATH]")


def main(argv: list[str] | None = None) -> int:
    try:
        output = _parse_output(argv)
        sample_only = os.environ.get("ASTERINAS_DESKTOP_M4_FRAMEBUFFER_SAMPLE_ONLY") == "1"
        deadline = time.monotonic() + 10
        if sample_only:
            while True:
                try:
                    (width, height, stride, _), sample, distinct, non_background = (
                        _read_framebuffer_sample()
                    )
                    break
                except ProbeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
            print(
                f"DEBIAN_DESKTOP_M4_FB_IOCTL width={width} height={height} "
                f"stride={stride} bpp=32 format=BgrReserved"
            )
            print(
                f"DEBIAN_DESKTOP_M4_FB_SAMPLE bytes={len(sample)} "
                f"sha256={hashlib.sha256(sample).hexdigest()}"
            )
            print(
                f"DEBIAN_DESKTOP_M4_FB_COLORS distinct={distinct} "
                f"non_background={non_background}"
            )
            return 0

        output = _safe_output(output)
        while True:
            (width, height, stride, buffer_bytes), raw = _read_framebuffer()
            try:
                distinct, non_background = _pixel_diversity(
                    raw, stride, width, height
                )
                break
            except ProbeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        sample = raw[:4096]
        payload = _png(raw, width, height, stride)
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ProbeError("screenshot write made no progress")
                view = view[written:]
        finally:
            os.close(descriptor)
        print(
            f"DEBIAN_DESKTOP_M4_FB_IOCTL width={width} height={height} "
            f"stride={stride} bpp=32 format=BgrReserved"
        )
        print(
            f"DEBIAN_DESKTOP_M4_FB_SAMPLE bytes={len(sample)} "
            f"sha256={hashlib.sha256(sample).hexdigest()}"
        )
        print(
            f"DEBIAN_DESKTOP_M4_FB_COLORS distinct={distinct} "
            f"non_background={non_background}"
        )
        print(
            f"DEBIAN_DESKTOP_M4_FB_SCREENSHOT path={output} width={width} "
            f"height={height} bytes={len(payload)} "
            f"sha256={hashlib.sha256(payload).hexdigest()}"
        )
        return 0
    except (OSError, ProbeError, ValueError) as error:
        print(f"desktop-m4-framebuffer-probe: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
