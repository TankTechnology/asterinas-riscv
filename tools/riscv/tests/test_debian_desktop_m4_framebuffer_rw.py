# SPDX-License-Identifier: MPL-2.0

import contextlib
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools.riscv.debian.rootfs import desktop_m4_framebuffer_rw as rw
from tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe import _metadata


class FramebufferWriteRestoreTests(unittest.TestCase):
    def test_small_transaction_preserves_padding_and_every_other_pixel(self):
        import struct

        width, height, stride = 48, 24, 48 * 4 + 32
        variable, fixed = _metadata(width, height)
        fixed = bytearray(fixed)
        struct.pack_into("=I", fixed, 24, stride * height)
        struct.pack_into("=I", fixed, 48, stride)
        original = bytes(range(256)) * (stride * height // 256)
        original += bytes(range((stride * height) % 256))
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "framebuffer"
            device.write_bytes(original)
            flushes = []

            class Mapping(bytearray):
                def flush(self):
                    flushes.append(bytes(self))
                def close(self):
                    device.write_bytes(self)

            def ioctl(_fd, command, buffer, _mutate):
                buffer[:] = variable if command == rw.FBIOGET_VSCREENINFO else fixed

            with patch.object(rw.stat, "S_ISCHR", return_value=True), \
                    patch.object(rw.fcntl, "ioctl", side_effect=ioctl), \
                    patch.object(rw.mmap, "mmap", return_value=Mapping(original)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rw.run(device), 0)
            self.assertEqual(len(flushes), 2)
            painted = flushes[0]
            expected_pixel = b"\x22\x66\xcc\xff"
            for row in range(height):
                start = row * stride
                changed = 16 * 4 if row < 16 else 0
                if changed:
                    self.assertEqual(painted[start:start + changed], expected_pixel * 16)
                self.assertEqual(painted[start + changed:start + stride], original[start + changed:start + stride])
            self.assertEqual(flushes[1], original)
            self.assertEqual(device.read_bytes(), original)

    def test_short_save_fails_before_mapping_or_any_write(self):
        variable, fixed = _metadata(48, 16)
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "framebuffer"
            original = bytes(48 * 16 * 4)
            device.write_bytes(original)

            def ioctl(_fd, command, buffer, _mutate):
                buffer[:] = variable if command == rw.FBIOGET_VSCREENINFO else fixed

            with patch.object(rw.stat, "S_ISCHR", return_value=True), \
                    patch.object(rw.fcntl, "ioctl", side_effect=ioctl), \
                    patch.object(rw.os, "pread", return_value=bytes(63)), \
                    patch.object(rw.mmap, "mmap") as mapping, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rw.run(device), 2)
                mapping.assert_not_called()
            self.assertEqual(device.read_bytes(), original)

    def test_rectangle_is_restored_when_first_flush_fails(self):
        width, height = 48, 16
        variable, fixed = _metadata(width, height)
        original = bytes(range(256)) * (width * height * 4 // 256)
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "framebuffer"
            device.write_bytes(original)
            class Mapping(bytearray):
                flushed = False
                def flush(self):
                    if not self.flushed:
                        self.flushed = True
                        raise OSError("flush failure")
                def close(self):
                    device.write_bytes(self)
            mapping = Mapping(original)
            def ioctl(_fd, command, buffer, _mutate):
                buffer[:] = variable if command == rw.FBIOGET_VSCREENINFO else fixed
            with patch.object(rw.stat, "S_ISCHR", return_value=True), \
                    patch.object(rw.fcntl, "ioctl", side_effect=ioctl), \
                    patch.object(rw.mmap, "mmap", return_value=mapping), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rw.run(device), 2)
            self.assertEqual(device.read_bytes(), original)

    def test_pattern_reaches_three_colors_and_restores_after_interruption(self):
        width, height = 48, 16
        variable, fixed = _metadata(width, height)
        original = bytes(range(256)) * (width * height * 4 // 256)
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "framebuffer"
            device.write_bytes(original)
            observed = []

            def ioctl(_fd, command, buffer, _mutate):
                buffer[:] = variable if command == rw.FBIOGET_VSCREENINFO else fixed
                return 0

            def hold(_seconds):
                pixels = device.read_bytes()
                observed.append(len(set(pixels[n:n + 4] for n in range(0, len(pixels), 4))))
                raise InterruptedError("capture interrupted")

            with patch.object(rw.stat, "S_ISCHR", return_value=True), \
                    patch.object(rw.fcntl, "ioctl", side_effect=ioctl), \
                    patch.object(rw.time, "sleep", side_effect=hold), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rw.run_pattern(device, 1), 2)
            self.assertEqual(observed, [3])
            self.assertEqual(device.read_bytes(), original)

    def test_layout_rejects_short_aperture_and_excessive_stride(self):
        import struct

        variable, fixed = _metadata(48, 16)
        shortened = bytearray(fixed)
        struct.pack_into("=I", shortened, 24, 1)
        with self.assertRaises(rw.FramebufferError):
            rw.layout(variable, shortened)
        excessive = bytearray(fixed)
        struct.pack_into("=I", excessive, 48, 2**32 - 1)
        with self.assertRaises(rw.FramebufferError):
            rw.layout(variable, excessive)

    def test_nonfinite_hold_rejected_before_open(self):
        with patch.object(rw.os, "open") as opening:
            for seconds in (float("nan"), float("inf"), 0, 31):
                with self.assertRaises(rw.FramebufferError):
                    rw.run_pattern(Path("/dev/fb0"), seconds)
            opening.assert_not_called()


if __name__ == "__main__":
    unittest.main()
