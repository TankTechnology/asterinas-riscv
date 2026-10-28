#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.megrez_board_session import *


class MilestoneDetectionTests(unittest.TestCase):
    def test_debug_root_console_profile_uses_exact_readiness_marker(self):
        self.assertEqual(
            board.FINAL_MILESTONE_MARKERS["debug-root-console"],
            "ASTERINAS_DEBUG_CONSOLE_READY uid=0",
        )

    def test_session_retains_only_the_current_boot_milestone_transcript(self):
        session = _make_session()
        session.note_milestone("Enter riscv_boot\n")
        session.note_milestone("ASTERINAS_DEBUG_CONSOLE_READY uid=0\n")
        self.assertEqual(
            session.milestone_transcript,
            "Enter riscv_boot\nASTERINAS_DEBUG_CONSOLE_READY uid=0\n",
        )

        session.start_boot_attempt()

        self.assertEqual(session.milestone_transcript, "")

    def test_session_can_reuse_a_caller_owned_serial_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "serial.log"
            with mock.patch.object(board, "open_serial") as open_serial:
                session = board.BoardSession.from_fd(
                    17,
                    str(log),
                    confirm=False,
                    final_marker="READY",
                )
            try:
                self.assertEqual(session.fd, 17)
                self.assertFalse(session.confirm)
                self.assertEqual(session._markers["userspace"], "READY")
                open_serial.assert_not_called()
            finally:
                session.log.close()

        stream = io.StringIO()
        session = board.BoardSession.from_fd(
            18,
            None,
            confirm=False,
            log_stream=stream,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            session._log("streamed")
        self.assertEqual(stream.getvalue(), "streamed")

    def test_all_milestones_match_their_markers(self):
        samples = {
            "kernel_enter": "U-Boot 2024.01-gdbb5f9e3 ... Starting kernel ...\nEnter riscv_boot\n",
            "banner": "    .:-. Presented by the Asterinas developers\n",
            "userspace": ">>> Hello from RISC-V userspace on Asterinas! <<<\n",
        }
        for name, text in samples.items():
            self.assertIn(board.MILESTONES[name], text, name)

        session = _make_session()
        with self.assertRaisesRegex(RuntimeError, "out of order"):
            session.note_milestone(
                "Hello from RISC-V userspace\n"
                "Presented by the Asterinas developers\n"
                "Enter riscv_boot\n"
            )
        self.assertEqual(session.milestones, {})

    def test_unrelated_text_does_not_match(self):
        for marker in board.MILESTONES.values():
            self.assertNotIn(marker, "random u-boot noise line\n")

    def test_uboot_gate_pattern(self):
        match = board.GATE_PATTERN.search("U-Boot 2024.01-gdbb5f9e3 (Mar 2024)")
        self.assertEqual(match.group(1), "2024.01-gdbb5f9e3")

    def test_framebuffer_profile_uses_the_kernel_registration_marker(self):
        session = _make_session()
        session._markers["userspace"] = board.FINAL_MILESTONE_MARKERS[
            "firmware-framebuffer"
        ]
        session.note_milestone(
            "Enter riscv_boot\n"
            "Presented by the Asterinas developers\n"
            "Registered firmware framebuffer: base=0xfd800000, size=0x7e9000\n"
        )
        self.assertEqual(tuple(session.milestones), tuple(board.MILESTONES))

    def test_specific_profile_does_not_require_the_optional_banner(self):
        session = board.BoardSession.from_fd(
            -1,
            None,
            confirm=False,
            final_marker=board.FINAL_MILESTONE_MARKERS["verifier"],
            log_stream=io.StringIO(),
        )
        session.note_milestone(
            "Enter riscv_boot\nDEBIAN_VERIFY_PASS sha256=abc bytes=1073741824\n"
        )
        self.assertEqual(tuple(session.milestones), ("kernel_enter", "userspace"))


class ArgumentContractTests(unittest.TestCase):
    def test_uboot_staging_rejects_unprotected_writable_boot(self):
        prefix = "init=/init asterinas.mmc_write_partition2 asterinas.reboot_after=900"
        for bootargs in (
            prefix
            + " systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=850"
            + " -- --root-init=systemd",
            "init=/init asterinas.mmc_write_partition2"
            " systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=780"
            " -- --root-init=systemd",
        ):
            with self.subTest(bootargs=bootargs):
                with self.assertRaises(ValueError):
                    board.uboot_bootargs_commands(bootargs)

    def test_writable_partition_emergency_reboot_requires_earlier_sync_reboot(self):
        required = _required_args() + [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        prefix = (
            "console=tty0 loglevel=off init=/init "
            "asterinas.mmc_write_partition2 asterinas.reboot_after=900 "
        )
        tail = " -- --root-init=systemd --debug-console=root"
        for setenv in (
            "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=900 ",
            "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=850 ",
            "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=abc ",
            "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=780 "
            "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=780 ",
        ):
            with self.subTest(setenv=setenv):
                _parse_fails(required + ["--bootargs", prefix + setenv + tail])

        admitted = board.parse_args(
            required
            + [
                "--bootargs",
                prefix + "systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=780" + tail,
            ]
        )
        self.assertIn("ASTERINAS_SAFE_REBOOT_AFTER=780", admitted.bootargs)
        derived = board.parse_args(
            required + ["--bootargs", prefix.rstrip() + tail]
        )
        self.assertIn("asterinas.reboot_after=900", derived.bootargs)
        _parse_fails(
            required
            + [
                "--bootargs",
                prefix + tail + " systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=780",
            ]
        )

    def test_debug_root_console_profile_requires_both_boot_selectors(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
            "--final-profile",
            "debug-root-console",
        ]
        for bootargs in (
            "console=ttyS0 init=/init -- --root-init=systemd",
            "console=ttyS0 init=/init -- --debug-console=root",
            "console=ttyS0 init=/init -- --root-init=interactive --debug-console=root",
        ):
            with self.subTest(bootargs=bootargs):
                _parse_fails(_required_args() + crc_args + ["--bootargs", bootargs])

        args = board.parse_args(
            _required_args()
            + crc_args
            + [
                "--bootargs",
                "console=ttyS0 loglevel=off init=/init -- --root-init=systemd "
                "--debug-console=root",
            ]
        )
        self.assertEqual(args.final_profile, "debug-root-console")

    def test_debug_root_console_admits_isolated_volatile_desktop_contract(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
            "--final-profile",
            "debug-root-console",
        ]
        prefix = "console=tty0 loglevel=off init=/init -- --root-init=systemd "
        admitted = prefix + "--debug-console=isolated-root --volatile-home"
        args = board.parse_args(_required_args() + crc_args + ["--bootargs", admitted])
        self.assertEqual(args.final_profile, "debug-root-console")
        for rejected in (
            prefix + "--volatile-home --debug-console=isolated-root",
            prefix + "--debug-console=isolated-root --volatile-home --volatile-home",
        ):
            with self.subTest(rejected=rejected):
                _parse_fails(_required_args() + crc_args + ["--bootargs", rejected])

    def test_debug_root_console_admits_isolated_persistent_desktop_contract(self):
        args = board.parse_args(
            _required_args()
            + [
                "--expected-crc32",
                "booti=0123abcd,dtb=89abcdef,initrd=00000001",
                "--final-profile",
                "debug-root-console",
                "--bootargs",
                "console=tty0 loglevel=off init=/init -- "
                "--root-init=systemd --debug-console=isolated-root",
            ]
        )
        self.assertEqual(args.final_profile, "debug-root-console")

    def test_debug_root_console_admits_root_volatile_desktop_contract(self):
        args = board.parse_args(
            _required_args()
            + [
                "--expected-crc32",
                "booti=0123abcd,dtb=89abcdef,initrd=00000001",
                "--final-profile",
                "debug-root-console",
                "--bootargs",
                "console=tty0 loglevel=off init=/init -- "
                "--root-init=systemd --debug-console=root --volatile-home",
            ]
        )
        self.assertEqual(args.final_profile, "debug-root-console")

    def test_debug_root_console_profile_requires_one_exclusive_off_loglevel(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
            "--final-profile",
            "debug-root-console",
        ]
        suffix = "init=/init -- --root-init=systemd --debug-console=root"
        for bootargs in (
            f"console=ttyS0 {suffix}",
            f"console=ttyS0 loglevel=info {suffix}",
            f"console=ttyS0 loglevel=off loglevel=off {suffix}",
            f"console=ttyS0 loglevel=info loglevel=off {suffix}",
        ):
            with self.subTest(bootargs=bootargs):
                _parse_fails(_required_args() + crc_args + ["--bootargs", bootargs])

        args = board.parse_args(
            _required_args()
            + crc_args
            + ["--bootargs", f"console=ttyS0 loglevel=off {suffix}"]
        )
        self.assertEqual(args.final_profile, "debug-root-console")

    def test_physical_mode_parses_complete_crc_map(self):
        args = board.parse_args(
            _required_args()
            + ["--expected-crc32", "booti=0123abcd,dtb=89ABCDEF,initrd=00000001"]
        )
        self.assertEqual(
            args.expected_crc32,
            {"booti": "0123abcd", "dtb": "89abcdef", "initrd": "00000001"},
        )

    def test_physical_mode_requires_every_crc(self):
        _parse_fails(_required_args())
        _parse_fails(
            _required_args() + ["--expected-crc32", "booti=0123abcd,dtb=89abcdef"]
        )

    def test_crc_map_rejects_duplicates_unknown_names_and_bad_values(self):
        invalid = (
            "booti=0123abcd,booti=0123abcd,dtb=89abcdef,initrd=00000001",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001,other=12345678",
            "booti=123,dtb=89abcdef,initrd=00000001",
            "booti=0123abcd,dtb=89abcdef,initrd=xyzxyzxy",
            "booti=0123abcd,,dtb=89abcdef,initrd=00000001",
        )
        for spec in invalid:
            with self.subTest(spec=spec):
                _parse_fails(_required_args() + ["--expected-crc32", spec])

        unsafe_values = (
            ("--booti", "kernel;reset"),
            ("--dtb", "board\nreset.dtb"),
            ("--initrd", "initrd && reset"),
            ("--bootargs", "init=/init; reset"),
            ("--bootargs", 'init="/init"'),
            ("--bootargs", "init=/init && reset"),
            ("--bootargs", "init=/init | reset"),
            ("--bootargs", "init=/init `reset`"),
            ("--bootargs", "init=/init\rreset"),
            ("--bootargs", "init=/init\x01"),
        )
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        for flag, unsafe_value in unsafe_values:
            with (
                self.subTest(flag=flag, value=unsafe_value),
                mock.patch.object(
                    board, "open_serial", side_effect=AssertionError("serial opened")
                ) as open_serial,
                mock.patch.object(board.BoardSession, "send") as send,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                argv = _required_args() + crc_args
                if flag in argv:
                    argv[argv.index(flag) + 1] = unsafe_value
                else:
                    argv.extend((flag, unsafe_value))
                try:
                    result: int | str = board.main(argv)
                except SystemExit as error:
                    result = error.code
                except AssertionError:
                    result = "serial opened"
                self.assertEqual(result, 2)
                open_serial.assert_not_called()
                send.assert_not_called()

    def test_mock_mode_accepts_a_short_positive_timeout_without_crcs(self):
        args = board.parse_args(
            _required_args() + ["--mock-qemu", "--mock-timeout", "0.05"]
        )
        self.assertEqual(args.mock_timeout, 0.05)
        self.assertIsNone(args.expected_crc32)
        self.assertEqual(
            args.bootargs,
            "loglevel=info init=/init",
        )
        self.assertNotIn("cpu_no_boost_1_6ghz", args.bootargs.split())

        help_output = io.StringIO()
        with contextlib.redirect_stdout(help_output), self.assertRaises(SystemExit):
            board.parse_args(["--help"])
        self.assertIn("--expected-crc32", help_output.getvalue())
        self.assertNotIn("[--expected-crc32", help_output.getvalue())

    def test_mock_timeout_must_be_finite_and_positive(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                _parse_fails(
                    _required_args() + ["--mock-qemu", "--mock-timeout", value]
                )

    def test_tftp_transport_requires_safe_ipv4_configuration(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        args = board.parse_args(
            _required_args()
            + crc_args
            + [
                "--load-transport",
                "tftp",
                "--tftp-board-address",
                "10.100.19.200",
                "--tftp-server-address",
                "10.100.19.216",
                "--tftp-netmask",
                "255.255.248.0",
                "--uboot-timeout",
                "43200",
                "--milestone-timeout",
                "150",
            ]
        )
        self.assertEqual(args.load_transport, "tftp")
        self.assertEqual(args.tftp_board_address, "10.100.19.200")
        self.assertEqual(args.tftp_server_address, "10.100.19.216")
        self.assertEqual(args.tftp_netmask, "255.255.248.0")
        self.assertEqual(args.uboot_timeout, 43200)
        self.assertEqual(args.milestone_timeout, 150)

        for flag, value in (
            ("--tftp-board-address", "10.100.19.200; reset"),
            ("--tftp-server-address", "2001:db8::1"),
            ("--tftp-netmask", "255.255.999.0"),
        ):
            with self.subTest(flag=flag, value=value):
                _parse_fails(
                    _required_args()
                    + crc_args
                    + ["--load-transport", "tftp", flag, value]
                )

    def test_mmc_transport_accepts_only_one_explicit_device_partition(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        args = board.parse_args(
            _required_args() + crc_args + ["--mmc-device-partition", "1:3"]
        )
        self.assertEqual(args.mmc_device_partition, "1:3")

        for value in ("", "1", "1:0", "1:3; reset", "../1:3", "01:3"):
            with self.subTest(value=value):
                _parse_fails(
                    _required_args() + crc_args + ["--mmc-device-partition", value]
                )

        _parse_fails(
            _required_args()
            + crc_args
            + [
                "--load-transport",
                "tftp",
                "--mmc-device-partition",
                "1:3",
            ]
        )

    def test_ymodem_transport_requires_pinned_kernel_decompression_contract(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        args = board.parse_args(
            _required_args()
            + crc_args
            + [
                "--load-transport",
                "ymodem",
                "--ymodem-directory",
                "/tmp/serial artifacts",
                "--booti-compressed-crc32",
                "deadbeef",
                "--booti-uncompressed-size",
                "14482552",
            ]
        )
        self.assertEqual(args.load_transport, "ymodem")
        self.assertEqual(args.ymodem_directory, Path("/tmp/serial artifacts"))
        self.assertEqual(args.booti_compressed_crc32, "deadbeef")
        self.assertEqual(args.booti_uncompressed_size, 14482552)

        required = _required_args() + crc_args + ["--load-transport", "ymodem"]
        for missing_contract in (
            (),
            ("--ymodem-directory", "/tmp/serial"),
            (
                "--ymodem-directory",
                "/tmp/serial",
                "--booti-compressed-crc32",
                "deadbeef",
            ),
        ):
            with self.subTest(arguments=missing_contract):
                _parse_fails(required + list(missing_contract))

    def test_final_profile_is_closed(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        args = board.parse_args(
            _required_args() + crc_args + ["--final-profile", "firmware-framebuffer"]
        )
        self.assertEqual(args.final_profile, "firmware-framebuffer")
        installer = board.parse_args(
            _required_args() + crc_args + ["--final-profile", "installer"]
        )
        self.assertEqual(
            board.FINAL_MILESTONE_MARKERS[installer.final_profile],
            "DEBIAN_INSTALL_PASS",
        )
        verifier = board.parse_args(
            _required_args() + crc_args + ["--final-profile", "verifier"]
        )
        self.assertEqual(
            board.FINAL_MILESTONE_MARKERS[verifier.final_profile],
            "DEBIAN_VERIFY_PASS",
        )
        _parse_fails(
            _required_args() + crc_args + ["--final-profile", "arbitrary-marker"]
        )

    def test_drm_firmware_marker_matches_the_probe_that_emits_it(self):
        """One string, two modules: the session waits for what the probe prints.

        The marker is emitted by a C program, so on the Python side it exists
        only as the QEMU gate's completion line and the board session's copy of
        it. If the two drift the session waits for a line that never arrives
        and reports a timeout, which reads like the board failing rather than
        like a typo.

        The profile exists because that probe boots on the board unchanged: the
        session loads the kernel at 0x80200000 and the initrd at 0x83000000,
        which is exactly the layout the QEMU gate boots. It is also the only
        thing in this tree that reaches `/dev/dri/card0` on the board -- the
        readiness and graphics gates both stop at `/dev/fb0`.
        """

        import qemu_uboot_profiles

        self.assertEqual(
            board.FINAL_MILESTONE_MARKERS["drm-firmware"],
            qemu_uboot_profiles.DRM_FIRMWARE_READY_LINE.decode(),
        )
        args = board.parse_args(
            _required_args()
            + ["--expected-crc32", "booti=0123abcd,dtb=89abcdef,initrd=00000001"]
            + ["--final-profile", "drm-firmware"]
        )
        self.assertEqual(args.final_profile, "drm-firmware")

    def test_firmware_framebuffer_requires_tty0_before_serial_open(self):
        crc_args = [
            "--expected-crc32",
            "booti=0123abcd,dtb=89abcdef,initrd=00000001",
        ]
        invalid_bootargs = (
            "init=/init",
            "console=ttyS0 console=tty0 init=/init",
        )
        for bootargs in invalid_bootargs:
            with (
                self.subTest(bootargs=bootargs),
                mock.patch.object(
                    board, "open_serial", side_effect=AssertionError("serial opened")
                ) as open_serial,
            ):
                _parse_fails(
                    _required_args()
                    + crc_args
                    + ["--firmware-framebuffer", "--bootargs", bootargs]
                )
            open_serial.assert_not_called()

        args = board.parse_args(
            _required_args()
            + crc_args
            + [
                "--firmware-framebuffer",
                "--bootargs",
                "console=tty0 console=ttyS0 init=/init",
            ]
        )
        self.assertTrue(args.firmware_framebuffer)


if __name__ == "__main__":
    unittest.main()
