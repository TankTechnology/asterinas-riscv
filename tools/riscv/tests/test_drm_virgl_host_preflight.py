#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from drm.virgl_host_preflight import (  # noqa: E402
    inspect_qemu,
    run_runtime_smoke,
)


class VirglHostPreflightTests(unittest.TestCase):
    @staticmethod
    def make_qemu(directory: Path, *, display: str, devices: str) -> Path:
        qemu = directory / "qemu"
        qemu.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            '  "--version ") echo "QEMU emulator version 10.2.0";;\n'
            f'  "-display help") printf "%s\\n" {display!r};;\n'
            f'  "-device help") printf "%s\\n" {devices!r};;\n'
            '  *) exit 2;;\n'
            'esac\n'
        )
        qemu.chmod(0o755)
        return qemu

    @staticmethod
    def make_runtime_qemu(directory: Path, body: str) -> Path:
        qemu = directory / "runtime-qemu"
        qemu.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        qemu.chmod(0o755)
        return qemu

    def test_accepts_both_required_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qemu = self.make_qemu(
                Path(temporary),
                display="egl-headless\nnone",
                devices="virtio-gpu-gl-device\nvirtio-gpu-device",
            )
            result = inspect_qemu(qemu)
            self.assertTrue(result.passed, result.reason)
            self.assertTrue(result.has_egl_headless)
            self.assertTrue(result.has_virtio_gpu_gl)

    def test_rejects_plain_qemu_without_gl_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qemu = self.make_qemu(
                Path(temporary), display="none", devices="virtio-gpu-device"
            )
            result = inspect_qemu(qemu)
            self.assertFalse(result.passed)
            self.assertIn("egl-headless", result.reason)

    def test_rejects_gl_backend_without_gl_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qemu = self.make_qemu(
                Path(temporary), display="egl-headless", devices="virtio-gpu-device"
            )
            result = inspect_qemu(qemu)
            self.assertFalse(result.passed)
            self.assertIn("virtio-gpu-gl-device", result.reason)

    def test_missing_executable_is_fail_closed(self) -> None:
        result = inspect_qemu("/no/such/qemu-system-riscv64")
        self.assertFalse(result.passed)
        self.assertIn("version failed", result.reason)

    def test_runtime_smoke_accepts_a_process_that_stays_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qemu = self.make_runtime_qemu(
                Path(temporary),
                "import time\ntime.sleep(30)\n",
            )
            result = run_runtime_smoke(qemu, timeout=0.1)
            self.assertTrue(result.passed, result.reason)
            self.assertTrue(result.timed_out)

    def test_runtime_smoke_rejects_early_gl_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qemu = self.make_runtime_qemu(
                Path(temporary),
                "import sys\nprint('OpenGL is not supported by the display', file=sys.stderr)\nsys.exit(1)\n",
            )
            result = run_runtime_smoke(qemu, timeout=1.0)
            self.assertFalse(result.passed)
            self.assertIn("OpenGL is not supported", result.reason)


if __name__ == "__main__":
    unittest.main()
