# SPDX-License-Identifier: MPL-2.0

from pathlib import Path
import tempfile
import unittest

from tools.riscv.debian.rootfs.desktop_m4_drm_recovery_gate import (
    RECOVERY_BOOTARGS,
    RECOVERY_SECONDS,
    classify_desktop_m4_drm_recovery,
)
from tools.riscv.debian.rootfs.firefox_drm_recovery_gate import (
    FirefoxDrmRecoveryOperations,
    classify_firefox_drm_recovery,
)


def _first_boot() -> bytes:
    return b"\n".join(
        (
            b"OpenSBI v1.7",
            b"DEBIAN_DESKTOP_M4_UDEV state=active",
            b"DEBIAN_DESKTOP_M4_LOGIND state=active",
            b"DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
            b"DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
            b"DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped",
            b"DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none",
            b"DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu",
            b"DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128",
            b"DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl",
            b"DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
            b"DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm",
            b"PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa'",
            b"DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=" + b"a" * 64,
            b"DEBIAN_DESKTOP_M4_READY user=asterinas display=:0",
        )
    )


class DrmRecoveryGateTests(unittest.TestCase):
    def test_firefox_qemu_uses_multithreaded_tcg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                (root / name).write_bytes(name.encode())
            argv = FirefoxDrmRecoveryOperations._qemu_argv(
                uboot=root / "u-boot",
                boot_disk=root / "boot.ext4",
                root_disk=root / "root.ext2",
                monitor_socket=root / "monitor.sock",
            )
        self.assertIn("-accel", argv)
        self.assertEqual(argv[argv.index("-accel") + 1], "tcg,thread=multi")

    def test_firefox_gate_disables_the_desktop_browser_competitor(self):
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_BROWSER_ENABLED=0",
            FirefoxDrmRecoveryOperations.BOOTARGS,
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_M4_SKIP_DRM_X11_SCREENSHOT=1",
            FirefoxDrmRecoveryOperations.BOOTARGS,
        )

    def test_recovery_bootargs_have_one_sufficient_timer(self):
        self.assertGreaterEqual(RECOVERY_SECONDS, 120)
        self.assertEqual(RECOVERY_BOOTARGS.count("asterinas.reboot_after="), 1)
        self.assertIn(
            f"asterinas.reboot_after={RECOVERY_SECONDS}", RECOVERY_BOOTARGS
        )

    def test_requires_fresh_firmware_epoch_after_drm_ready(self):
        transcript = _first_boot() + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_desktop_m4_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertTrue(result.passed)

    def test_rejects_drm_ready_without_recovery(self):
        result = classify_desktop_m4_drm_recovery(
            _first_boot(), expected_debian_release="13.7"
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "missing post-desktop OpenSBI epoch")

    def test_firefox_classifier_requires_ordered_virgl_receipts(self):
        transcript = _first_boot() + b"\n".join(
            (
                b"FIREFOX_DRM_SUPPORT compositing=WebRender webrender=available renderer=virgl source=browser-log",
                b"FIREFOX_DRM_GL renderer=virgl",
                b"FIREFOX_DRM_SCREENSHOT width=1152 height=584 sha256=" + b"b" * 64,
                b"FIREFOX_DRM_FRAMES samples=16 min_ms=1.0 p50_ms=2.0 p95_ms=3.0 max_ms=4.0",
                b"FIREFOX_DRM_RECOVERY restart=pass",
            )
        ) + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_firefox_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertTrue(result.passed)

    def test_firefox_classifier_allows_m4_duplicate_screenshot_skip(self):
        first_boot = _first_boot().replace(
            b"DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256="
            + b"a" * 64,
            b"DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT skipped=firefox-scene-gate",
        )
        transcript = first_boot + b"\n".join(
            (
                b"FIREFOX_DRM_SUPPORT compositing=WebRender webrender=available renderer=virgl source=browser-log",
                b"FIREFOX_DRM_GL renderer=virgl",
                b"FIREFOX_DRM_SCREENSHOT width=1152 height=584 sha256=" + b"b" * 64,
                b"FIREFOX_DRM_FRAMES samples=16 min_ms=1.0 p50_ms=2.0 p95_ms=3.0 max_ms=4.0",
                b"FIREFOX_DRM_RECOVERY restart=pass",
            )
        ) + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_firefox_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertTrue(result.passed)

    def test_firefox_classifier_rejects_missing_web_render(self):
        transcript = _first_boot() + b"\n".join(
            (
                b"FIREFOX_DRM_SUPPORT compositing=llvmpipe webrender=available renderer=llvmpipe source=browser-log",
                b"FIREFOX_DRM_GL renderer=llvmpipe",
                b"FIREFOX_DRM_SCREENSHOT width=1152 height=584 sha256=" + b"b" * 64,
                b"FIREFOX_DRM_FRAMES samples=16 min_ms=1.0 p50_ms=2.0 p95_ms=3.0 max_ms=4.0",
                b"FIREFOX_DRM_RECOVERY restart=pass",
            )
        ) + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_firefox_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertFalse(result.passed)

    def test_firefox_classifier_accepts_generic_sanitizer_with_two_proofs(self):
        transcript = _first_boot() + b"\n".join(
            (
                b"FIREFOX_DRM_SUPPORT compositing=WebRender webrender=available renderer=Generic_Renderer source=browser-log",
                b"FIREFOX_DRM_GL renderer=Generic_Renderer",
                b"FIREFOX_DRM_GLXTEST rc=0 RENDERER virgl DRM_RENDERDEVICE /dev/dri/renderD128",
                b"FIREFOX_DRM_RECOVERY restart=pass",
            )
        ) + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_firefox_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertTrue(result.passed)
        self.assertIn("renderer", result.reason)
        self.assertIn("drm-render-node", result.reason)

    def test_firefox_classifier_requires_two_independent_proofs(self):
        transcript = _first_boot() + b"\n".join(
            (
                b"FIREFOX_DRM_SUPPORT compositing=Basic webrender=unavailable renderer=Generic_Renderer source=browser-log",
                b"FIREFOX_DRM_RECOVERY restart=pass",
            )
        ) + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_firefox_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertFalse(result.passed)
        self.assertIn("insufficient Firefox acceleration evidence", result.reason)

    def test_firefox_classifier_rejects_explicit_software_probe(self):
        transcript = _first_boot() + b"\n".join(
            (
                b"FIREFOX_DRM_SUPPORT compositing=WebRender webrender=available renderer=Generic_Renderer source=browser-log",
                b"FIREFOX_DRM_GL renderer=llvmpipe",
                b"FIREFOX_DRM_GLXTEST rc=0 RENDERER virgl DRM_RENDERDEVICE /dev/dri/renderD128",
                b"FIREFOX_DRM_RECOVERY restart=pass",
            )
        ) + b"\nOpenSBI v1.7\nU-Boot 2026.07\n=> "
        result = classify_firefox_drm_recovery(
            transcript, expected_debian_release="13.7"
        )
        self.assertFalse(result.passed)
        self.assertIn("software renderer", result.reason)


if __name__ == "__main__":
    unittest.main()
