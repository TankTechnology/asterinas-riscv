# SPDX-License-Identifier: MPL-2.0

import unittest

from tools.riscv.debian.rootfs.desktop_m4_x11_gate import classify_x11_desktop


class X11DesktopGateTests(unittest.TestCase):
    def test_classifier_requires_three_nonce_bound_samples_and_ready(self):
        transcript = b"\n".join(
            (
                b"DEBIAN_DESKTOP_M4_UDEV state=active",
                b"DEBIAN_DESKTOP_M4_LOGIND state=active",
                b"DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
                b"DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
                b"DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0",
                b"DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
                b"DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm",
                b"DEBIAN_DESKTOP_M4_X11_SAMPLE band=0 expected=cc6622 ximage=cc6622 fb0=cc6622",
                b"DEBIAN_DESKTOP_M4_X11_SAMPLE band=1 expected=22cc66 ximage=22cc66 fb0=22cc66",
                b"DEBIAN_DESKTOP_M4_X11_SAMPLE band=2 expected=6622cc ximage=6622cc fb0=6622cc",
                b"DEBIAN_DESKTOP_M4_X11_READY bands=3",
                b"DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 stride=5120 bpp=32 format=BgrReserved",
                b"DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=" + b"a" * 64,
                b"DEBIAN_DESKTOP_M4_FB_COLORS distinct=3 non_background=2688",
                b"DEBIAN_DESKTOP_M4_READY user=asterinas display=:0",
            )
        )
        result = classify_x11_desktop(transcript, expected_debian_release="13.7")
        self.assertTrue(result.passed)

    def test_classifier_rejects_mismatched_x11_sample(self):
        transcript = b"DEBIAN_DESKTOP_M4_X11_SAMPLE band=0 expected=cc6622 ximage=000000 fb0=cc6622\n"
        result = classify_x11_desktop(transcript, expected_debian_release="13.7")
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
