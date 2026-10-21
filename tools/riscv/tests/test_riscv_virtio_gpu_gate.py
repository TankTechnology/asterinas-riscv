# SPDX-License-Identifier: MPL-2.0

"""Inventory test for the RISC-V virtio-gpu gate and the shared KMS probe."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class VirtioGpuGateTests(unittest.TestCase):
    def test_gate_scripts_exist_and_print_usage(self) -> None:
        for relative in (
            "tools/riscv/drm/virtio_gpu_check.sh",
            "tools/riscv/drm/xorg_check.sh",
            "tools/drm/build_probe.sh",
        ):
            with self.subTest(script=relative):
                script = REPOSITORY_ROOT / relative
                self.assertTrue(script.is_file())
                self.assertTrue(os.access(script, os.X_OK), "not executable")
                result = run(str(script), "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage:", result.stdout + result.stderr)

    def test_gate_scripts_parse(self) -> None:
        for relative in (
            "tools/riscv/drm/virtio_gpu_check.sh",
            "tools/riscv/drm/xorg_check.sh",
            "tools/drm/build_probe.sh",
        ):
            with self.subTest(script=relative):
                result = run("-n", str(REPOSITORY_ROOT / relative))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_source_is_architecture_neutral(self) -> None:
        source = (REPOSITORY_ROOT / "tools/drm/virtio_gpu_probe.c").read_text()
        for ioctl in (
            "DRM_IOCTL_MODE_GETRESOURCES",
            "DRM_IOCTL_MODE_GETCONNECTOR",
            "DRM_IOCTL_MODE_GETENCODER",
            "DRM_IOCTL_MODE_CREATE_DUMB",
            "DRM_IOCTL_MODE_MAP_DUMB",
            "DRM_IOCTL_MODE_ADDFB",
            "DRM_IOCTL_MODE_SETCRTC",
            "DRM_IOCTL_MODE_DIRTYFB",
        ):
            with self.subTest(ioctl=ioctl):
                self.assertIn(ioctl, source)
        self.assertIn("DRMPROBE_PASS", source)

    def test_paint_client_is_self_contained(self) -> None:
        source = (REPOSITORY_ROOT / "tools/drm/x11_paint.c").read_text()
        self.assertIn("XPAINT_DONE", source)
        self.assertIn("/tmp/.X11-unix/X%u", source)
        # No libX11: the request encoding is spelled out in the source.
        self.assertNotIn("#include <X11/", source)


if __name__ == "__main__":
    unittest.main()
