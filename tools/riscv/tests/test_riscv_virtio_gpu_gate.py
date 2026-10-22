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
            "tools/drm/prime_check.sh",
            "tools/drm/initramfs_probe_check.sh",
            "tools/riscv/drm/graphical_check.sh",
            "tools/drm/conformance_matrix.sh",
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
            "tools/drm/prime_check.sh",
            "tools/drm/initramfs_probe_check.sh",
            "tools/riscv/drm/graphical_check.sh",
            "tools/drm/conformance_matrix.sh",
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

    def test_prime_probe_covers_the_acceptance(self) -> None:
        source = (REPOSITORY_ROOT / "tools/drm/prime_probe.c").read_text()
        for marker in (
            "DRM_IOCTL_PRIME_HANDLE_TO_FD",
            "DRM_IOCTL_PRIME_FD_TO_HANDLE",
            "SCM_RIGHTS",
            "PRIMEPROBE_PASS",
            "PRIMEPROBE_REJECT_NON_DMABUF",
            "PRIMEPROBE_REJECT_CLOSED_FD",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_p3_probes_cover_the_acceptance(self) -> None:
        atomic = (REPOSITORY_ROOT / "tools/drm/atomic_probe.c").read_text()
        for marker in (
            "DRM_IOCTL_MODE_ATOMIC",
            "DRM_CLIENT_CAP_ATOMIC",
            "DRM_MODE_ATOMIC_TEST_ONLY",
            "DRM_IOCTL_MODE_CREATEPROPBLOB",
            "ATOMICPROBE_COMMIT",
            "ATOMICPROBE_LEGACY",
            "ATOMICPROBE_FLIP_EVENT",
            "ATOMICPROBE_PASS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, atomic)

        flip = (REPOSITORY_ROOT / "tools/drm/flip_probe.c").read_text()
        for marker in (
            "DRM_IOCTL_MODE_PAGE_FLIP",
            "DRM_MODE_PAGE_FLIP_EVENT",
            "DRM_MODE_PAGE_FLIP_ASYNC",
            "DRM_IOCTL_WAIT_VBLANK",
            "DRM_VBLANK_EVENT",
            "DRM_EVENT_FLIP_COMPLETE",
            "FLIPPROBE_PASS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, flip)

        sync = (REPOSITORY_ROOT / "tools/drm/sync_probe.c").read_text()
        for marker in (
            "DRM_IOCTL_SYNCOBJ_CREATE",
            "DRM_IOCTL_SYNCOBJ_WAIT",
            "DRM_IOCTL_SYNCOBJ_TIMELINE_SIGNAL",
            "DRM_IOCTL_SYNCOBJ_QUERY",
            "SYNCPROBE_CROSS_PROCESS",
            "SYNCPROBE_PASS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, sync)

    def test_dri3_probe_requires_a_buffer_exchange(self) -> None:
        source = (REPOSITORY_ROOT / "tools/drm/dri3_probe.c").read_text()
        for marker in (
            "X11_QUERY_EXTENSION",
            "DRI3_PIXMAP_FROM_BUFFER",
            "SCM_RIGHTS",
            "DRI3PROBE_PASS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_sysfs_probe_covers_the_libdrm_contract(self) -> None:
        source = (REPOSITORY_ROOT / "tools/drm/sysfs_probe.c").read_text()
        for marker in (
            "/sys/dev/char/%u:%u/device/drm",
            "/sys/dev/char/%u:%u/device/subsystem",
            "MODALIAS",
            "DEVNAME",
            "SYSFSPROBE_PASS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_paint_client_is_self_contained(self) -> None:
        source = (REPOSITORY_ROOT / "tools/drm/x11_paint.c").read_text()
        self.assertIn("XPAINT_DONE", source)
        self.assertIn("/tmp/.X11-unix/X%u", source)
        # No libX11: the request encoding is spelled out in the source.
        self.assertNotIn("#include <X11/", source)


if __name__ == "__main__":
    unittest.main()
