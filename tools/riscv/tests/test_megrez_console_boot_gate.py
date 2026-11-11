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
import os
import tempfile
import unittest

from tools.riscv.board.megrez_console import (
    BootSafetyError,
    boot_load_commands,
    _initramfs_entries,
    _read_md_word,
    arm_hardware_watchdog,
    run_command_line,
    validate_boot_append,
    validate_initramfs_for_mode,
)


KERNEL_BASE = "console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2"


class BootSafetyGateTest(unittest.TestCase):
    def test_bounded_deadline_and_basic_mode_pass(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=1800 -- --root-init=basic",
            allow_experimental_mode=True,
            allow_long_deadline=True,
        )

    def test_systemd_mode_with_deadline_passes(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=systemd",
            allow_long_deadline=True,
        )

    def test_proven_mode_with_a_short_deadline_needs_no_flags(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=300 -- --root-init=systemd"
        )

    def test_a_deadline_inside_the_userspace_guard_is_refused(self) -> None:
        """180 s or less turns into an immediate userspace reboot.

        `megrez-safe-reboot` reboots 180 s before the kernel deadline; at or
        below that guard the subtraction collapses and aborts the boot (the
        2026-11-11 S2 boot).
        """
        for seconds in (1, 120, 180):
            with self.assertRaises(BootSafetyError) as caught:
                validate_boot_append(
                    f"{KERNEL_BASE} asterinas.reboot_after={seconds} -- "
                    "--root-init=systemd"
                )
            self.assertIn("userspace watchdog guard", str(caught.exception))

    def test_an_unproven_mode_needs_an_explicit_opt_in(self) -> None:
        with self.assertRaises(BootSafetyError) as caught:
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=300 -- --root-init=basic"
            )
        self.assertIn("allow-experimental-mode", str(caught.exception))

    def test_a_long_deadline_needs_an_explicit_opt_in(self) -> None:
        with self.assertRaises(BootSafetyError) as caught:
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=5400 -- --root-init=systemd"
            )
        self.assertIn("allow-long-deadline", str(caught.exception))

    def test_missing_deadline_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(f"{KERNEL_BASE} -- --root-init=systemd")

    def test_missing_deadline_passes_with_kernel_default_acknowledged(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} -- --root-init=systemd",
            allow_kernel_default=True,
            allow_long_deadline=True,
        )

    def test_zero_deadline_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=0 -- --root-init=systemd"
            )

    def test_non_numeric_deadline_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=soon -- --root-init=systemd",
                allow_long_deadline=True,
            )

    def test_deadline_beyond_one_day_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=90000 -- --root-init=systemd",
                allow_long_deadline=True,
            )

    def test_interactive_mode_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError) as caught:
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=interactive",
                allow_experimental_mode=True,
                allow_long_deadline=True,
            )
        self.assertIn("ASTER_DEBIANROOT", str(caught.exception))

    def test_interactive_mode_can_be_allowed_explicitly(self) -> None:
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=300 -- --root-init=interactive",
            allow_interactive=True,
            allow_experimental_mode=True,
        )

    def test_unknown_root_init_mode_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 -- --root-init=shell",
                allow_experimental_mode=True,
                allow_long_deadline=True,
            )

    def test_missing_root_init_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(f"{KERNEL_BASE} asterinas.reboot_after=900 --")

    def test_missing_separator_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 --root-init=systemd"
            )

    def test_two_separators_are_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 -- -- --root-init=systemd"
            )

    def test_duplicate_root_init_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=900 "
                "-- --root-init=systemd --root-init=basic"
            )

    def test_an_unknown_init_option_is_refused(self) -> None:
        """The 2026-11-05 wedge: a stray token after '--' made stage1 hold."""
        with self.assertRaises(BootSafetyError) as caught:
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=300 "
                "-- --root-init=systemd --debug-console=bogus"
            )
        self.assertIn("root-init-argument", str(caught.exception))

    def test_a_duplicate_init_option_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=300 "
                "-- --root-init=systemd --volatile-home --volatile-home"
            )

    def test_debug_console_needs_systemd_mode(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_boot_append(
                f"{KERNEL_BASE} asterinas.reboot_after=300 "
                "-- --root-init=basic --debug-console=root",
                allow_experimental_mode=True,
            )
        validate_boot_append(
            f"{KERNEL_BASE} asterinas.reboot_after=300 "
            "-- --root-init=systemd --debug-console=isolated-root"
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
            " asterinas.reboot_after=300",
            " asterinas.reboot_after=900",
            " asterinas.reboot_after=86400",
            " asterinas.reboot_after=86401",
            " asterinas.reboot_after=soon",
            " asterinas.reboot_after=-5",
        ]
        safe_modes = {"basic", "systemd", "probe", "probe-auto"}
        accepted_deadlines = {
            " asterinas.reboot_after=1",
            " asterinas.reboot_after=300",
            " asterinas.reboot_after=900",
            " asterinas.reboot_after=86400",
        }

        for separator, mode, deadline in itertools.product(
            separators, modes, deadlines
        ):
            mode_tokens = f" --root-init={mode}" if mode else ""
            append = f"{KERNEL_BASE}{deadline}{separator}{mode_tokens}".strip()
            try:
                # Both opt-ins make the whole grammar reachable; with them the
                # accepted set still has to stay recoverable.
                validate_boot_append(
                    append,
                    allow_experimental_mode=True,
                    allow_long_deadline=True,
                )
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


class RunMarkerTest(unittest.TestCase):
    def test_the_echo_cannot_match_the_marker(self) -> None:
        """A marker that appears in the typed line matches before execution."""
        line = run_command_line("md.l 0x508000fc 1", "ZZOK4321")
        self.assertNotIn("ZZOK4321", line)
        self.assertIn("echo ZZOK${__ASTERINAS_RUN_MARKER__}4321", line)

    def test_the_shell_expands_the_marker_to_the_expected_text(self) -> None:
        line = run_command_line("true", "ZZOK99")
        self.assertTrue(line.endswith("ZZOK${__ASTERINAS_RUN_MARKER__}99"))


class FakeConsole:
    """Feeds canned `run` results and records the commands that were sent."""

    def __init__(self, responses: list[tuple[str, bool]]) -> None:
        self.responses = list(responses)
        self.commands: list[str] = []

    def run(self, command: str, timeout: float, char_pause: float = 0.0):
        self.commands.append(command)
        return self.responses.pop(0)


class WatchdogArmTest(unittest.TestCase):
    CLOCK = ("", True)
    ARMED = ("", True)
    COMPONENT_OK = ("508000fc: 44570120 00000000 00000000 00000000", True)
    COMPONENT_MISSING = ("508000fc: 00000000 00000000 00000000 00000000", True)
    CONTROL_ENABLED = ("50800000: 0000001f 00000000 00000000 00000000", True)
    CONTROL_DISABLED = ("50800000: 00000000 00000000 00000000 00000000", True)

    def test_reads_the_first_word_of_md_output(self) -> None:
        text = "508000fc: 44570120 00000000 00000000 00000000"
        self.assertEqual(_read_md_word(text, 0x5080_00FC), 0x4457_0120)
        self.assertIsNone(_read_md_word("no match here", 0x5080_00FC))

    def test_arms_and_verifies_the_watchdog(self) -> None:
        con = FakeConsole(
            [self.CLOCK, self.COMPONENT_OK, self.ARMED, self.CONTROL_ENABLED]
        )
        arm_hardware_watchdog(con, 10.0, allow_missing=False)
        self.assertIn("0x51828444", con.commands[0])
        self.assertIn("mw.l 0x50800004 0xf", con.commands[2])
        self.assertIn("mw.l 0x50800000 0x1f", con.commands[2])

    def test_a_missing_watchdog_is_refused(self) -> None:
        con = FakeConsole([self.CLOCK, self.COMPONENT_MISSING])
        with self.assertRaises(SystemExit):
            arm_hardware_watchdog(con, 10.0, allow_missing=False)

    def test_a_missing_watchdog_can_be_allowed_for_virt(self) -> None:
        con = FakeConsole([self.CLOCK, self.COMPONENT_MISSING])
        arm_hardware_watchdog(con, 10.0, allow_missing=True)

    def test_a_watchdog_that_stays_disabled_is_refused(self) -> None:
        con = FakeConsole(
            [self.CLOCK, self.COMPONENT_OK, self.ARMED, self.CONTROL_DISABLED]
        )
        with self.assertRaises(SystemExit):
            arm_hardware_watchdog(con, 10.0, allow_missing=False)

    def test_an_unreadable_control_word_is_refused(self) -> None:
        con = FakeConsole([self.CLOCK, self.COMPONENT_OK, self.ARMED, ("garbage", False)])
        with self.assertRaises(SystemExit):
            arm_hardware_watchdog(con, 10.0, allow_missing=False)


class BootLoadCommandsTest(unittest.TestCase):
    APPEND = "console=ttyS0 init=/init asterinas.reboot_after=420 -- --root-init=systemd"

    def test_default_initramfs_comes_from_p1(self) -> None:
        steps = boot_load_commands(
            "stage1-daily.cpio", "megrez-daily.dtb", "/root/kernel-linux4.img",
            self.APPEND,
        )
        self.assertEqual(steps[0], "ext4load mmc 1:1 0x88300000 stage1-daily.cpio")

    def test_a_candidate_initramfs_can_come_from_p2(self) -> None:
        """Trying a new stage1 must not require writing to the boot partition."""
        steps = boot_load_commands(
            "/root/stage1-heartbeat.cpio", "megrez-daily.dtb",
            "/root/kernel-linux4.img", self.APPEND, "mmc 1:2",
        )
        self.assertEqual(
            steps[0], "ext4load mmc 1:2 0x88300000 /root/stage1-heartbeat.cpio"
        )
        self.assertEqual(steps[2], "ext4load mmc 1:1 0x88000000 megrez-daily.dtb")
        self.assertEqual(steps[3], "ext4load mmc 1:2 0x80200000 /root/kernel-linux4.img")

    def test_an_unknown_device_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            boot_load_commands(
                "stage1-daily.cpio", "megrez-daily.dtb", "/root/kernel-img",
                self.APPEND, "mmc 0:1",
            )


class InitramfsModeTest(unittest.TestCase):
    def _archive(self, entries: dict[str, bytes]) -> str:
        handle, path = tempfile.mkstemp(suffix=".cpio")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "wb") as stream:
            stream.write(newc(entries))
        return path

    def test_parses_newc_names(self) -> None:
        path = self._archive({"init": b"x", "bin/busybox": b"y"})
        self.assertEqual(_initramfs_entries(path), {"init", "bin/busybox"})

    def test_basic_without_busybox_is_refused(self) -> None:
        path = self._archive({"init": b"x"})
        with self.assertRaises(BootSafetyError) as caught:
            validate_initramfs_for_mode("basic", path)
        self.assertIn("busybox", str(caught.exception))

    def test_basic_with_busybox_is_accepted(self) -> None:
        path = self._archive({"init": b"x", "bin/busybox": b"y"})
        validate_initramfs_for_mode("basic", path)

    def test_basic_without_a_local_copy_is_refused(self) -> None:
        with self.assertRaises(BootSafetyError):
            validate_initramfs_for_mode("basic", None)

    def test_systemd_needs_no_archive_check(self) -> None:
        validate_initramfs_for_mode("systemd", None)


def newc(entries: dict[str, bytes]) -> bytes:
    """Builds a minimal newc cpio archive for the parser tests."""
    out = bytearray()
    for ino, (name, data) in enumerate(entries.items(), start=1):
        name_bytes = name.encode() + b"\0"
        fields = [ino, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name_bytes), 0]
        out += b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
        out += name_bytes
        out += b"\0" * (-len(out) % 4)
        out += data
        out += b"\0" * (-len(out) % 4)
    trailer = b"TRAILER!!!\0"
    fields = [0] * 13
    fields[11] = len(trailer)
    out += b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
    out += trailer
    out += b"\0" * (-len(out) % 4)
    return bytes(out)


if __name__ == "__main__":
    unittest.main()