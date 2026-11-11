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

from tools.riscv.board.megrez_console import (
    BootSafetyError,
    _read_md_word,
    arm_hardware_watchdog,
    validate_boot_append,
)


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


class FakeConsole:
    """Feeds canned `run` results and records the commands that were sent."""

    def __init__(self, responses: list[tuple[str, bool]]) -> None:
        self.responses = list(responses)
        self.commands: list[str] = []

    def run(self, command: str, timeout: float, char_pause: float = 0.0):
        self.commands.append(command)
        return self.responses.pop(0)


class WatchdogArmTest(unittest.TestCase):
    def test_reads_the_first_word_of_md_output(self) -> None:
        text = "508000fc: 44570120 00000000 00000000 00000000"
        self.assertEqual(_read_md_word(text, 0x5080_00FC), 0x4457_0120)
        self.assertIsNone(_read_md_word("no match here", 0x5080_00FC))

    def test_arms_and_verifies_the_watchdog(self) -> None:
        con = FakeConsole(
            [
                ("508000fc: 44570120 00000000 00000000 00000000", True),
                ("", True),
                ("50800000: 0000001f 00000000 00000000 00000000", True),
            ]
        )
        arm_hardware_watchdog(con, 10.0, allow_missing=False)
        self.assertIn("mw.l 0x50800004 0xf", con.commands[1])
        self.assertIn("mw.l 0x50800000 0x1f", con.commands[1])

    def test_a_missing_watchdog_is_refused(self) -> None:
        con = FakeConsole([("508000fc: 00000000 00000000 00000000 00000000", True)])
        with self.assertRaises(SystemExit):
            arm_hardware_watchdog(con, 10.0, allow_missing=False)

    def test_a_missing_watchdog_can_be_allowed_for_virt(self) -> None:
        con = FakeConsole([("508000fc: 00000000 00000000 00000000 00000000", True)])
        arm_hardware_watchdog(con, 10.0, allow_missing=True)

    def test_a_watchdog_that_stays_disabled_is_refused(self) -> None:
        con = FakeConsole(
            [
                ("508000fc: 44570120 00000000 00000000 00000000", True),
                ("", True),
                ("50800000: 00000000 00000000 00000000 00000000", True),
            ]
        )
        with self.assertRaises(SystemExit):
            arm_hardware_watchdog(con, 10.0, allow_missing=False)

    def test_an_unreadable_control_word_is_refused(self) -> None:
        con = FakeConsole(
            [
                ("508000fc: 44570120 00000000 00000000 00000000", True),
                ("", True),
                ("garbage", False),
            ]
        )
        with self.assertRaises(SystemExit):
            arm_hardware_watchdog(con, 10.0, allow_missing=False)


if __name__ == "__main__":
    unittest.main()