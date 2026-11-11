"""Boot command-line safety gate for ad-hoc Megrez boots.

The 2026-11-11 wedge: a boot used `--root-init=interactive`, which only
matches a rootfs labelled `ASTER_DEBIANROOT`, on an image labelled
`ASTER_BROWSERWEB`, and carried no `asterinas.reboot_after` deadline. Stage1
held in `fail_and_hold()`, nothing in userspace or the kernel could recover,
and the board needed a physical reset. These tests pin the checks that make
that combination impossible to boot by accident.
"""

from __future__ import annotations

import itertools
import unittest

from tools.riscv.board.megrez_console import BootSafetyError, validate_boot_append


KERNEL_BASE = "console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2"


class BootSafetyGateTest(unittest.TestCase):
    def test_bounded_deadline_and_basic_mode_pass(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=1800 -- --root-init=basic"
        )

    def test_systemd_mode_with_deadline_passes(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=systemd"
        )

    def test_missing_deadline_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(f"{KERNEL_BASE} -- --root-init=basic")

    def test_missing_deadline_passes_with_kernel_default_acknowledged(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} -- --root-init=basic", allow_kernel_default=True
        )

    def test_zero_deadline_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=0 -- --root-init=basic"
            )

    def test_non_numeric_deadline_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=soon -- --root-init=basic"
            )

    def test_deadline_beyond_one_day_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=90000 -- --root-init=basic"
            )

    def test_interactive_mode_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError) as caught:
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=interactive"
            )
        self.assertIn("ASTER_DEBIANROOT", str(caught.exception))

    def test_interactive_mode_can_be_allowed_explicitly(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=interactive",
            allow_interactive=True,
        )

    def test_unknown_root_init_mode_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=shell"
            )

    def test_missing_root_init_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(f"{KERNEL_BASE} asterinas.reboot_after=900 --")

    def test_missing_separator_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 --root-init=basic"
            )

    def test_two_separators_are_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 -- -- --root-init=basic"
            )

    def test_duplicate_root_init_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 "
                "-- --root-init=basic --root-init=systemd"
            )

    def test_every_accepted_line_is_recoverable(self) -> None:
        """Exhaustive check over the interesting token combinations.

        The accepted set must stay a subset of the recoverable set: a safe
        root-init mode and either a bounded deadline or an acknowledged
        kernel default. This is the executable form of the policy, so a
        future change that widens the gate has to widen this test too.
        """

        separators = ["", " --", " -- --"]
        modes = ["", "basic", "systemd", "probe", "probe-auto", "interactive", "shell"]
        deadlines = [
            "",
            " asterinas.reboot_after=0",
            " asterinas.reboot_after=1",
            " asterinas.reboot_after=900",
            " asterinas.reboot_after=86400",
            " asterinas.reboot_after=86401",
            " asterinas.reboot_after=soon",
            " asterinas.reboot_after=-5",
        ]
        safe_modes = {"basic", "systemd", "probe", "probe-auto"}
        accepted_deadlines = {
            " asterinas.reboot_after=1",
            " asterinas.reboot_after=900",
            " asterinas.reboot_after=86400",
        }

        for separator, mode, deadline in itertools.product(
            separators, modes, deadlines
        ):
            mode_tokens = f" --root-init={mode}" if mode else ""
            append = f"{KERNEL_BASE}{deadline}{separator}{mode_tokens}".strip()
            try:
                validate_boot_append(append)
            except BootSafetyError:
                continue

            self.assertIn(
                mode,
                safe_modes,
                msg=f"accepted a root-init mode this rootfs cannot satisfy: {append!r}",
            )
            self.assertIn(separator, (" --",), msg=f"accepted {append!r}")
            self.assertIn(
                deadline,
                accepted_deadlines,
                msg=f"accepted a missing or out-of-bounds deadline: {append!r}",
            )


if __name__ == "__main__":
    unittest.main()
