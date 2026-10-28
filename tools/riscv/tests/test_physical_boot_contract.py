#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Freeze the physical desktop/stability kernel command lines."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.riscv import physical_boot_contract as contract
from tools.riscv import megrez_boot_stability as stability
from tools.riscv import megrez_desktop as desktop
from tools.riscv import megrez_physical_graphics as graphics


ROOTFS = Path(__file__).resolve().parents[1]
PHYSICAL = (
    "console=tty0 loglevel=off asterinas.klog_capture=info init=/init "
    "asterinas.mmc_write_partition2 asterinas.reboot_after=1140 "
    "systemd.mask=asterinas-browser-web-evidence.service "
    "systemd.mask=asterinas-desktop-network.service "
    "systemd.mask=serial-getty@ttyS0.service "
    "systemd.mask=console-getty.service "
    "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1 "
    "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy "
    "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1 "
    "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9 "
    "-- --root-init=systemd --debug-console=isolated-root --volatile-home"
)
DESKTOP_START = (
    "console=tty0 loglevel=off asterinas.klog_capture=info init=/init "
    "asterinas.mmc_write_partition2 "
    "systemd.mask=asterinas-browser-web-evidence.service "
    "systemd.mask=asterinas-desktop-network.service "
    "systemd.mask=serial-getty@ttyS0.service "
    "systemd.mask=console-getty.service "
    "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1 "
    "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy "
    "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1 "
    "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9 "
    "-- --root-init=systemd --debug-console=isolated-root --volatile-home"
)


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        bootargs=(
            "console=ttyS0 init=/init asterinas.mmc_write_partition2 "
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
            "-- --root-init=systemd"
        ),
        plan_sha256="a" * 64,
        validate=lambda: None,
    )


class PhysicalBootContractTests(unittest.TestCase):
    def test_physical_composer_matches_the_frozen_golden(self) -> None:
        self.assertEqual(contract.physical_bootargs(), PHYSICAL)
        self.assertEqual(graphics.physical_bootargs(_plan()), PHYSICAL)

    def test_boot_stability_keeps_recovery_but_drops_network_inputs(self) -> None:
        value = stability.boot_stability_bootargs(_plan())

        self.assertEqual(value, PHYSICAL)
        self.assertEqual(value.split().count("--"), 1)
        self.assertEqual(value.split().count("asterinas.mmc_write_partition2"), 1)
        self.assertIn("asterinas.reboot_after=1140", value.split())

    def test_desktop_start_drops_recovery_but_keeps_the_local_surface(self) -> None:
        value = desktop.desktop_start_bootargs(_plan())

        self.assertEqual(value, DESKTOP_START)
        self.assertNotIn("asterinas.reboot_after=", value)
        self.assertEqual(value.split().count("asterinas.mmc_write_partition2"), 1)
        self.assertTrue(value.endswith("--volatile-home"))

    def test_filtering_rejects_a_noncanonical_base(self) -> None:
        with self.assertRaises(ValueError):
            contract.boot_stability_bootargs("console=tty0")
        with self.assertRaises(ValueError):
            contract.desktop_start_bootargs("console=tty0")

    def test_physical_modules_do_not_redefine_the_base_tokens(self) -> None:
        for path in (
            ROOTFS / "megrez_physical_graphics.py",
            ROOTFS / "megrez_boot_stability.py",
            ROOTFS / "megrez_desktop.py",
        ):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("console=tty0", source)
                self.assertNotIn("asterinas.mmc_write_partition2", source)
                self.assertNotIn("asterinas.reboot_after=", source)
