#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Freeze the QEMU/systemd kernel command lines in one composer."""

from __future__ import annotations

from pathlib import Path
import unittest

from tools.riscv.debian.rootfs import daily_use_gate
from tools.riscv.debian.rootfs import desktop_m3_gate
from tools.riscv.debian.rootfs import desktop_m4_gate
from tools.riscv.debian.rootfs import desktop_m5_network_gate
from tools.riscv.debian.rootfs import desktop_m5_qemu_gate
from tools.riscv.debian.rootfs import systemd_m2_gate
from tools.riscv.debian.rootfs.gate_protocol import systemd_bootargs


ROOTFS = Path(__file__).resolve().parents[1] / "debian/rootfs"
BASE = "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd"
M4 = (
    "console=ttyS0 loglevel=4 "
    "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
    "init=/init -- --root-init=systemd"
)
DAILY = (
    "console=ttyS0 loglevel=4 init=/init "
    "systemd.unit=multi-user.target -- --root-init=systemd"
)
M5 = (
    "console=ttyS0 loglevel=4 init=/init "
    "asterinas.debian_network=qemu-slirp "
    "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL="
    "http://10.0.2.2:17894/asterinas-network-probe.bin "
    "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_SIZE=65536 "
    "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_SHA256="
    "7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2 "
    "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_REQUESTS=20 "
    "systemd.setenv=ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS=120 "
    "-- --root-init=systemd"
)


class BootargsComposerTests(unittest.TestCase):
    def test_composer_preserves_the_order_of_pre_and_post_init_tokens(self) -> None:
        self.assertEqual(systemd_bootargs(), BASE)
        self.assertEqual(
            systemd_bootargs(pre_init=("one=a",), post_init=("two=b",)),
            "console=ttyS0 loglevel=4 one=a init=/init two=b "
            "-- --root-init=systemd",
        )
        self.assertEqual(
            systemd_bootargs(
                init_args=("--root-fs=ext4", "--root-init=systemd")
            ),
            "console=ttyS0 loglevel=4 init=/init "
            "-- --root-fs=ext4 --root-init=systemd",
        )

    def test_composer_rejects_noncanonical_tokens(self) -> None:
        with self.assertRaises(ValueError):
            systemd_bootargs(pre_init=("bad token",))
        with self.assertRaises(ValueError):
            systemd_bootargs(init_args=("--root-fs=ext4",))
        with self.assertRaises(ValueError):
            systemd_bootargs(init_args=("--root-init=systemd", "--root-init=systemd"))

    def test_named_bootargs_match_frozen_goldens(self) -> None:
        self.assertEqual(systemd_m2_gate.SYSTEMD_M2_BOOTARGS, BASE)
        self.assertEqual(
            systemd_m2_gate.SYSTEMD_EXT4_M3_BOOTARGS,
            "console=ttyS0 loglevel=4 init=/init "
            "-- --root-fs=ext4 --root-init=systemd",
        )
        self.assertEqual(desktop_m3_gate.DESKTOP_M3_BOOTARGS, BASE)
        self.assertEqual(desktop_m4_gate.DesktopM4Operations.BOOTARGS, M4)
        self.assertEqual(daily_use_gate.DAILY_USE_BOOTARGS, DAILY)
        self.assertEqual(desktop_m5_qemu_gate.DESKTOP_M5_QEMU_BOOTARGS, M5)

    def test_network_variants_match_frozen_goldens(self) -> None:
        proxy = desktop_m5_qemu_gate.qemu_web_network_bootargs(
            desktop_m5_network_gate.NetworkMode.PROXY
        )
        direct = desktop_m5_qemu_gate.qemu_web_network_bootargs(
            desktop_m5_network_gate.NetworkMode.DIRECT
        )
        tls = desktop_m5_qemu_gate.qemu_web_network_bootargs(
            desktop_m5_network_gate.NetworkMode.DIRECT,
            expected_failure=desktop_m5_qemu_gate.QemuExpectedFailure.TLS,
        )

        self.assertEqual(proxy.split().count("--"), 1)
        self.assertEqual(direct.split().count("--"), 1)
        self.assertEqual(tls.split().count("--"), 1)
        self.assertTrue(proxy.startswith("console=ttyS0 loglevel=4 init=/init "))
        self.assertIn("ASTERINAS_WEB_NETWORK_MODE=proxy", proxy)
        self.assertIn("ASTERINAS_WEB_NETWORK_MODE=direct", direct)
        self.assertIn("ASTERINAS_WEB_NETWORK_RESOLVER=10.0.2.3", direct)
        self.assertIn("ASTERINAS_WEB_NETWORK_HTTPS_URL=", tls)
        self.assertNotIn("ASTERINAS_WEB_NETWORK_RESOLVER=192.0.2.1", direct)
        for value in (proxy, direct, tls):
            self.assertTrue(value.endswith("-- --root-init=systemd"))

    def test_shared_gate_modules_do_not_redefine_the_base_line(self) -> None:
        modules = (
            "systemd_m2_gate.py",
            "desktop_m3_gate.py",
            "desktop_m4_gate.py",
            "daily_use_gate.py",
            "desktop_m5_qemu_gate.py",
        )
        for name in modules:
            with self.subTest(module=name):
                source = (ROOTFS / name).read_text(encoding="utf-8")
                self.assertNotIn("console=ttyS0", source)
