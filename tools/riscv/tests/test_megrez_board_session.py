"""Unit tests for the guarded Megrez board session contract."""

import contextlib
import io
import os
import socket
import tempfile
import threading
import time
import tty
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import megrez_board_session as board


def _required_args() -> list[str]:
    return ["serial", "--booti", "kernel", "--initrd", "initrd", "--dtb", "board.dtb"]


def _parse_fails(args: list[str]) -> None:
    with (
        contextlib.redirect_stderr(io.StringIO()),
        unittest.TestCase().assertRaises(SystemExit),
    ):
        board.parse_args(args)


def _make_session() -> board.BoardSession:
    session = board.BoardSession.__new__(board.BoardSession)
    session.fd = -1
    session.confirm = False
    session.milestones = {}
    session._milestone_tail = ""
    session._next_milestone = 0
    session._markers = dict(board.MILESTONES)
    session._log = mock.Mock()
    return session


class MilestoneDetectionTests(unittest.TestCase):
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
        self.assertEqual(args.bootargs, board.DEFAULT_BOOTARGS)

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


class FirmwareFramebufferContractTests(unittest.TestCase):
    def test_megrez_contract_matches_the_physically_verified_scanout(self):
        framebuffer = board.MEGREZ_FRAMEBUFFER
        self.assertEqual(framebuffer.address, 0xFD80_0000)
        self.assertEqual(framebuffer.size, 0x7E9000)
        self.assertEqual(framebuffer.width, 1920)
        self.assertEqual(framebuffer.height, 1080)
        self.assertEqual(framebuffer.stride, 7680)
        self.assertEqual(framebuffer.pixel_format, "x8r8g8b8")

        invalid = (
            {"address": True},
            {"address": 0},
            {"size": 1920 * 1080 * 4 - 1},
            {"stride": 1920 * 4 - 1},
            {"pixel_format": "a8r8g8b8"},
            {"address": (1 << 64) - 1},
        )
        values = {
            "address": 0xFD80_0000,
            "size": 0x7E9000,
            "width": 1920,
            "height": 1080,
            "stride": 7680,
            "pixel_format": "x8r8g8b8",
        }
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                board.FramebufferHandoff(**(values | override))

    def test_commands_are_ram_only_and_use_64_bit_reg_cells(self):
        commands = board.MEGREZ_FRAMEBUFFER.commands()
        self.assertEqual(commands[0], "fdt mknode / framebuffer@fd800000")
        self.assertIn(
            "fdt set /framebuffer@fd800000 reg <0x0 0xfd800000 0x0 0x7e9000>",
            commands,
        )
        self.assertEqual(commands[-1], "fdt print /framebuffer@fd800000")
        self.assertFalse(any("saveenv" in command for command in commands))


class MockQemuContractTests(unittest.TestCase):
    def _run(
        self, chunks: list[bytes], timeout: float, *, hold_connection_open: bool = False
    ) -> tuple[int, float]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serial.sock"
            release_connection = threading.Event()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                server.settimeout(0.5)

                def serve() -> None:
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        return
                    with connection:
                        try:
                            for chunk in chunks:
                                connection.sendall(chunk)
                                time.sleep(0.005)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        if hold_connection_open:
                            release_connection.wait(timeout=1)

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                started = time.monotonic()
                try:
                    result = board.main(
                        [
                            str(path),
                            "--booti",
                            "kernel",
                            "--initrd",
                            "initrd",
                            "--dtb",
                            "board.dtb",
                            "--mock-qemu",
                            "--mock-timeout",
                            str(timeout),
                        ]
                    )
                    elapsed = time.monotonic() - started
                finally:
                    release_connection.set()
                    thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                return result, elapsed

    def test_split_markers_complete_early(self):
        chunks = [
            b"Enter ris",
            b"cv_boot\nPresented by the Asterinas ",
            b"developers\nHello from RISC-V user",
            b"space\n",
        ]
        result, elapsed = self._run(chunks, timeout=0.5)
        self.assertEqual(result, 0)
        self.assertLess(elapsed, 0.4)

        invalid_sequences = (
            [
                b"Hello from RISC-V userspace\n",
                b"Presented by the Asterinas developers\n",
                b"Enter riscv_boot\n",
            ],
            [
                b"Presented by the Asterinas developers\n",
                b"Enter riscv_boot\n",
                b"Presented by the Asterinas developers\n",
                b"Hello from RISC-V userspace\n",
            ],
        )
        for invalid in invalid_sequences:
            with self.subTest(sequence=invalid):
                result, elapsed = self._run(invalid, timeout=0.2)
                self.assertEqual(result, 2)
                self.assertLess(elapsed, 0.5)

    def test_missing_milestone_fails_within_the_requested_timeout(self):
        chunks = [b"Enter riscv_boot\n", b"Presented by the Asterinas developers\n"]
        result, elapsed = self._run(chunks, timeout=0.05, hold_connection_open=True)
        self.assertEqual(result, 2)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.5)

        class ConnectTimeoutSocket:
            def __init__(self):
                self.timeout_at_connect: float | None = None
                self.current_timeout: float | None = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, timeout: float) -> None:
                self.current_timeout = timeout

            def connect(self, _device: str) -> None:
                self.timeout_at_connect = self.current_timeout
                raise socket.timeout("connect timed out")

        timed_out_socket = ConnectTimeoutSocket()
        with (
            mock.patch.object(socket, "socket", return_value=timed_out_socket),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                result = board.main(
                    _required_args() + ["--mock-qemu", "--mock-timeout", "0.05"]
                )
            except socket.timeout:
                result = "raised socket.timeout"
        self.assertEqual(result, 2)
        self.assertEqual(timed_out_socket.timeout_at_connect, 0.05)

        with tempfile.TemporaryDirectory() as tmp:
            missing_socket = str(Path(tmp) / "missing.sock")
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    result = board.main(
                        [
                            missing_socket,
                            "--booti",
                            "kernel",
                            "--initrd",
                            "initrd",
                            "--dtb",
                            "board.dtb",
                            "--mock-qemu",
                            "--mock-timeout",
                            "0.05",
                        ]
                    )
                except OSError:
                    result = "raised OSError"
        self.assertEqual(result, 2)


class SerialContractTests(unittest.TestCase):
    def _session(self) -> board.BoardSession:
        return _make_session()

    def test_wait_for_logs_every_new_chunk_once(self):
        session = self._session()
        logged: list[str] = []
        session._log = logged.append
        with mock.patch.object(
            board, "read_available", side_effect=["first", "second:done"]
        ):
            output = session.wait_for("done", timeout=0.2)
        self.assertEqual(output, "firstsecond:done")
        self.assertEqual(logged, ["first", "second:done"])

    def test_wait_for_respects_outer_deadline_with_a_real_pty(self):
        master, slave = os.openpty()
        tty.setraw(slave)
        session = self._session()
        session.fd = slave
        logged: list[str] = []
        session._log = logged.append

        def write_marker() -> None:
            os.write(master, b"pty-marker")

        thread = threading.Thread(target=write_marker)
        started = time.monotonic()
        try:
            thread.start()
            output = session.wait_for("pty-marker", timeout=0.2)
        finally:
            thread.join(timeout=1)
            os.close(master)
            os.close(slave)
        self.assertEqual(output, "pty-marker")
        self.assertEqual("".join(logged), "pty-marker")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_wait_for_uboot_prompt_interrupts_split_autoboot_once(self):
        session = self._session()
        with (
            mock.patch.object(
                board,
                "read_available",
                side_effect=["Hit any key to stop auto", "boot:  2\n", "=> "],
            ),
            mock.patch.object(board.os, "write", return_value=1) as write,
        ):
            output = session.wait_for_uboot_prompt(timeout=0.2)

        self.assertEqual(output, "Hit any key to stop autoboot:  2\n=> ")
        write.assert_called_once_with(-1, b" ")

    def test_command_rejects_an_address_from_the_wrong_echo(self):
        session = self._session()
        command = "ext4load mmc 1:1 0x80200000 /kernel"
        session.send = mock.Mock()
        session.wait_for = mock.Mock(
            return_value="ext4load mmc 1:1 0x80200000 /wrong\r\n=> "
        )
        with self.assertRaisesRegex(RuntimeError, "echo"):
            session.command(command, expect="0x80200000")

    def test_command_rejects_uboot_error_output(self):
        session = self._session()
        command = "ext4load mmc 1:1 0x80200000 /kernel"
        session.send = mock.Mock()
        session.wait_for = mock.Mock(
            return_value=f"{command}\r\n** File not found /kernel **\r\n=> "
        )
        with self.assertRaisesRegex(RuntimeError, "U-Boot error"):
            session.command(command)

        fdt_command = "fdt set /chosen asterinas,usb-host /soc/usb@50480000"
        session.wait_for = mock.Mock(
            return_value=(
                f"{fdt_command}\r\nlibfdt fdt_setprop(): FDT_ERR_NOSPACE\r\n=> "
            )
        )
        with self.assertRaisesRegex(RuntimeError, "FDT error"):
            session.command(fdt_command)

    def test_booti_accepts_nonfatal_fdt_warning_after_kernel_entry(self):
        session = self._session()
        command = "booti 0x80200000 0x83000000:${initrd_size} 0xf0000000"
        session.send = mock.Mock()
        session.wait_for = mock.Mock(
            return_value=(
                f"{command}\r\n"
                "ERROR: reserving fdt memory region failed "
                "(addr=fffff000 size=1000 flags=4)\r\n"
                "Starting kernel ...\r\nEnter riscv_boot\r\n"
            )
        )

        output = session.command(
            command, expect=board.MILESTONES["kernel_enter"], timeout=30
        )

        self.assertIn(board.MILESTONES["kernel_enter"], output)

    def test_load_artifact_requires_load_evidence_not_just_echo(self):
        session = self._session()
        load = "ext4load mmc 1:1 0x80200000 /kernel"
        session.command = mock.Mock(return_value=f"{load}\r\n=> ")
        with self.assertRaisesRegex(RuntimeError, "bytes read"):
            session.load_artifact("booti", "kernel", 0x80200000, "0123abcd")

    def test_load_artifact_verifies_crc_address_and_value(self):
        session = self._session()
        load = "ext4load mmc 1:1 0x80200000 /kernel"
        crc = "crc32 0x80200000 ${filesize}"
        session.command = mock.Mock(
            side_effect=[
                f"{load}\r\n1234 bytes read in 1 ms\r\n=> ",
                f"{crc}\r\nCRC32 for 80200000 ... ==> 0123abcd\r\n=> ",
            ]
        )
        size = session.load_artifact("booti", "kernel", 0x80200000, "0123abcd")
        self.assertEqual(size, 1234)
        self.assertEqual(
            session.command.call_args_list,
            [mock.call(load), mock.call(crc)],
        )

    def test_load_artifact_rejects_wrong_crc_or_address(self):
        for result in (
            "CRC32 for 80200000 ... ==> deadbeef",
            "CRC32 for 83000000 ... ==> 0123abcd",
        ):
            with self.subTest(result=result):
                session = self._session()
                session.command = mock.Mock(
                    side_effect=["1234 bytes read in 1 ms", result]
                )
                with self.assertRaisesRegex(RuntimeError, "CRC32"):
                    session.load_artifact("booti", "kernel", 0x80200000, "0123abcd")

    def test_tftp_load_requires_transfer_size_and_exact_crc(self):
        session = self._session()
        load = "tftpboot 0x80200000 kernel"
        crc = "crc32 0x80200000 ${filesize}"
        session.command = mock.Mock(
            side_effect=[
                f"{load}\r\nBytes transferred = 14288192 (da0600 hex)\r\n=> ",
                f"{crc}\r\nCRC32 for 80200000 ... ==> e5a5fac5\r\n=> ",
            ]
        )

        size = session.load_tftp_artifact("booti", "kernel", 0x80200000, "e5a5fac5")

        self.assertEqual(size, 14288192)
        self.assertEqual(
            session.command.call_args_list,
            [mock.call(load, timeout=120), mock.call(crc)],
        )

        session.command = mock.Mock(return_value=f"{load}\r\n=> ")
        with self.assertRaisesRegex(RuntimeError, "positive transfer"):
            session.load_tftp_artifact("booti", "kernel", 0x80200000, "e5a5fac5")

    def test_ymodem_load_switches_baud_and_verifies_pinned_source(self):
        session = self._session()
        session.fd = 41
        session.send = mock.Mock()
        session.wait_for = mock.Mock(
            side_effect=[
                "loady 83000000 1500000\r\n"
                "## Switch baudrate to 1500000 bps and press ENTER ...",
                "## Ready for binary (ymodem) download to 0x83000000 at 1500000 bps...\r\n",
                "## Total Size = 0x00000004 = 4 Bytes\r\n"
                "## Switch baudrate to 115200 bps and press ESC ...",
                "=> ",
            ]
        )
        session.command = mock.Mock(
            return_value=(
                "crc32 0x83000000 0x4\r\n"
                "CRC32 for 83000000 ... 83000003 ==> 0123abcd\r\n=> "
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "installer.cpio"
            source.write_bytes(b"data")
            observed: list[tuple[int, int]] = []

            def transfer(serial_fd: int, source_fd: int, timeout: float) -> None:
                observed.append((serial_fd, os.fstat(source_fd).st_size))
                self.assertEqual(timeout, 120.0)

            with (
                mock.patch.object(board, "_set_serial_baud") as set_baud,
                mock.patch.object(board, "_transfer_ymodem_file", side_effect=transfer),
                mock.patch.object(board.os, "write", return_value=1) as write,
            ):
                size = session.load_ymodem_artifact(
                    "initrd",
                    Path(directory),
                    "installer.cpio",
                    0x83000000,
                    "0123abcd",
                )

        self.assertEqual(size, 4)
        self.assertEqual(observed, [(41, 4)])
        self.assertEqual(
            set_baud.call_args_list,
            [mock.call(41, 1_500_000), mock.call(41, board.BAUD)],
        )
        self.assertEqual(
            write.call_args_list, [mock.call(41, b"\r"), mock.call(41, b"\x1b")]
        )
        self.assertEqual(
            session.wait_for.call_args_list,
            [
                mock.call("press ENTER", timeout=15),
                mock.call("Ready for binary", timeout=15),
                mock.call("press ESC", timeout=15),
                mock.call(board.PROMPT, timeout=15),
            ],
        )
        session.command.assert_called_once_with("crc32 0x83000000 0x4")

    def test_ymodem_sender_uses_known_good_u_boot_1k_mode(self):
        serial_read, serial_write = os.pipe()
        with tempfile.NamedTemporaryFile() as source:
            source_fd = source.fileno()
            completed = board.subprocess.CompletedProcess([], 0, stderr=b"")
            with mock.patch.object(
                board.subprocess, "run", return_value=completed
            ) as run:
                board._transfer_ymodem_file(serial_write, source_fd, 12.5)
        os.close(serial_read)
        os.close(serial_write)

        run.assert_called_once_with(
            ["/usr/bin/sb", "-k", f"/proc/self/fd/{source_fd}"],
            stdin=serial_write,
            stdout=serial_write,
            stderr=board.subprocess.PIPE,
            pass_fds=(source_fd,),
            timeout=12.5,
            check=False,
        )

    def test_ymodem_failure_cancels_receiver_and_restores_prompt(self):
        session = self._session()
        session.fd = 41
        session.send = mock.Mock()
        session.wait_for = mock.Mock(
            side_effect=[
                "loady 83000000 1500000\r\npress ENTER",
                "Ready for binary",
                "=> ",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "installer.cpio"
            source.write_bytes(b"data")
            with (
                mock.patch.object(board, "_set_serial_baud") as set_baud,
                mock.patch.object(
                    board,
                    "_transfer_ymodem_file",
                    side_effect=RuntimeError("sender failed"),
                ),
                mock.patch.object(board.os, "write", return_value=1) as write,
                self.assertRaisesRegex(RuntimeError, "sender failed"),
            ):
                session.load_ymodem_artifact(
                    "initrd",
                    Path(directory),
                    "installer.cpio",
                    0x83000000,
                    "0123abcd",
                )

        self.assertEqual(
            write.call_args_list,
            [
                mock.call(41, b"\r"),
                mock.call(41, b"\x18" * 8),
                mock.call(41, b"\x1b\r"),
            ],
        )
        self.assertEqual(
            set_baud.call_args_list,
            [mock.call(41, board.YMODEM_BAUD), mock.call(41, board.BAUD)],
        )
        self.assertEqual(
            session.wait_for.call_args_list,
            [
                mock.call("press ENTER", timeout=15),
                mock.call("Ready for binary", timeout=15),
                mock.call(board.PROMPT, timeout=15),
            ],
        )

    def test_ymodem_lzma_load_verifies_decompressed_kernel_identity(self):
        session = self._session()
        session.load_ymodem_artifact = mock.Mock(return_value=1234)
        session.command = mock.Mock(
            side_effect=[
                "lzmadec 0x90000000 0x80200000\r\n=> ",
                "crc32 0x80200000 0xdcfc78\r\n"
                "CRC32 for 80200000 ... 80def677 ==> 4c2d6451\r\n=> ",
            ]
        )

        session.load_ymodem_lzma_artifact(
            "booti",
            Path("/tmp/artifacts"),
            "kernel.lzma",
            0x90000000,
            0x80200000,
            "c77daf81",
            14_482_552,
            "4c2d6451",
        )

        session.load_ymodem_artifact.assert_called_once_with(
            "booti-compressed",
            Path("/tmp/artifacts"),
            "kernel.lzma",
            0x90000000,
            "c77daf81",
        )
        self.assertEqual(
            session.command.call_args_list,
            [
                mock.call("lzmadec 0x90000000 0x80200000", timeout=60),
                mock.call("crc32 0x80200000 0xdcfc78"),
            ],
        )


class BootTransactionTests(unittest.TestCase):
    def test_every_artifact_is_loaded_and_verified_before_booti(self):
        events: list[tuple] = []
        session = mock.Mock()
        session.start_boot_attempt.side_effect = lambda: events.append(("start",))
        session.load_artifact.side_effect = lambda *args: events.append(("load", *args))
        session.command.side_effect = lambda command, **kwargs: (
            events.append(("command", command, kwargs)) or "boot output"
        )
        args = SimpleNamespace(
            booti="kernel",
            dtb="board.dtb",
            initrd="initrd",
            bootargs="init=/init",
            expected_crc32={
                "booti": "0123abcd",
                "dtb": "89abcdef",
                "initrd": "00000001",
            },
            firmware_framebuffer=False,
        )

        output = board.boot_loaded_artifacts(session, args)

        self.assertEqual(output, "boot output")
        self.assertEqual(
            [event for event in events if event[0] == "load"],
            [
                ("load", "booti", "kernel", 0x80200000, "0123abcd"),
                ("load", "dtb", "board.dtb", 0xF0000000, "89abcdef"),
                ("load", "initrd", "initrd", 0x83000000, "00000001"),
            ],
        )
        booti_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "command" and event[1].startswith("booti ")
        )
        self.assertTrue(
            all(
                index < booti_index
                for index, event in enumerate(events)
                if event[0] == "load"
            )
        )
        self.assertIn(
            (
                "command",
                "fdt set /chosen asterinas,usb-host "
                "/soc/usb0@50480000/dwc3@50480000 "
                "/soc/usb1@50490000/dwc3@50490000",
                {},
            ),
            events,
        )
        self.assertEqual(events[booti_index - 1], ("start",))
        session.start_boot_attempt.assert_called_once_with()

        current_boot = "Enter riscv_boot\nPresented by the Asterinas developers\nHello from RISC-V userspace\n"
        stale_preload = f"{current_boot}{board.PROMPT}"
        physical_session = mock.Mock()
        physical_session.wait_for_uboot_prompt.return_value = stale_preload
        physical_session.milestones = {}
        physical_session.log = mock.Mock()
        physical_session.fd = -1

        def record_current_attempt(text: str) -> None:
            if text == current_boot:
                physical_session.milestones.update(
                    {name: float(index) for index, name in enumerate(board.MILESTONES)}
                )

        physical_session.note_milestone.side_effect = record_current_attempt
        with (
            mock.patch.object(board, "BoardSession", return_value=physical_session),
            mock.patch.object(
                board, "boot_loaded_artifacts", return_value=current_boot
            ),
            mock.patch.object(board.os, "close"),
        ):
            result = board.main(
                _required_args()
                + [
                    "--expected-crc32",
                    "booti=0123abcd,dtb=89abcdef,initrd=00000001",
                ]
            )
        self.assertEqual(result, 0)
        physical_session.send.assert_called_once_with("")
        self.assertLess(
            physical_session.mock_calls.index(mock.call.send("")),
            physical_session.mock_calls.index(
                mock.call.wait_for_uboot_prompt(timeout=60.0)
            ),
        )
        physical_session.note_milestone.assert_called_once_with(current_boot)

    def test_framebuffer_handoff_is_complete_before_booti(self):
        events: list[str] = []
        session = mock.Mock()
        session.load_artifact.return_value = 4096
        session.command.side_effect = lambda command, **_kwargs: (
            events.append(command) or "boot output"
        )
        args = SimpleNamespace(
            booti="kernel",
            dtb="board.dtb",
            initrd="initrd",
            bootargs="console=tty0 init=/init",
            expected_crc32={
                "booti": "0123abcd",
                "dtb": "89abcdef",
                "initrd": "00000001",
            },
            firmware_framebuffer=True,
        )

        board.boot_loaded_artifacts(session, args)

        resize_index = events.index("fdt resize 0x1000")
        booti_index = next(
            index
            for index, command in enumerate(events)
            if command.startswith("booti ")
        )
        framebuffer_commands = list(board.MEGREZ_FRAMEBUFFER.commands())
        self.assertEqual(
            events[resize_index + 1 : resize_index + 1 + len(framebuffer_commands)],
            framebuffer_commands,
        )
        self.assertLess(resize_index, booti_index)
        self.assertTrue(
            all(events.index(command) < booti_index for command in framebuffer_commands)
        )

    def test_tftp_transport_is_configured_without_persistent_environment(self):
        events: list[tuple] = []
        session = mock.Mock()
        session.load_tftp_artifact.side_effect = lambda *args: events.append(
            ("tftp", *args)
        )
        session.command.side_effect = lambda command, **kwargs: (
            events.append(("command", command, kwargs)) or "boot output"
        )
        args = SimpleNamespace(
            booti="kernel",
            dtb="board.dtb",
            initrd="initrd",
            bootargs="init=/init asterinas.reboot_after=180",
            expected_crc32={
                "booti": "0123abcd",
                "dtb": "89abcdef",
                "initrd": "00000001",
            },
            firmware_framebuffer=False,
            load_transport="tftp",
            tftp_board_address="10.100.19.200",
            tftp_server_address="10.100.19.216",
            tftp_netmask="255.255.248.0",
        )

        board.boot_loaded_artifacts(session, args)

        self.assertEqual(
            events[:3],
            [
                ("command", "setenv ipaddr 10.100.19.200", {}),
                ("command", "setenv serverip 10.100.19.216", {}),
                ("command", "setenv netmask 255.255.248.0", {}),
            ],
        )
        self.assertEqual(
            [event for event in events if event[0] == "tftp"],
            [
                ("tftp", "booti", "kernel", 0x80200000, "0123abcd"),
                ("tftp", "dtb", "board.dtb", 0xF0000000, "89abcdef"),
                ("tftp", "initrd", "initrd", 0x83000000, "00000001"),
            ],
        )
        self.assertFalse(any("saveenv" in str(event) for event in events))

    def test_ymodem_transport_uses_mmc_dtb_and_explicit_initrd_size(self):
        events: list[tuple] = []
        session = mock.Mock()
        session.load_ymodem_lzma_artifact.side_effect = lambda *args: events.append(
            ("lzma", *args)
        )
        session.load_artifact.side_effect = lambda *args: events.append(("mmc", *args))
        session.load_ymodem_artifact.side_effect = lambda *args: (
            events.append(("ymodem", *args)) or 886829
        )
        session.command.side_effect = lambda command, **kwargs: (
            events.append(("command", command, kwargs)) or "boot output"
        )
        args = SimpleNamespace(
            booti="kernel.lzma",
            dtb="dtbs/current/board.dtb",
            initrd="installer.cpio.gz",
            bootargs="init=/init asterinas.reboot_after=600",
            expected_crc32={
                "booti": "4c2d6451",
                "dtb": "4afcb20e",
                "initrd": "d1b80054",
            },
            firmware_framebuffer=False,
            load_transport="ymodem",
            ymodem_directory=Path("/tmp/artifacts"),
            booti_compressed_crc32="c77daf81",
            booti_uncompressed_size=14_482_552,
        )

        board.boot_loaded_artifacts(session, args)

        self.assertEqual(
            [event for event in events if event[0] in ("lzma", "mmc", "ymodem")],
            [
                (
                    "lzma",
                    "booti",
                    Path("/tmp/artifacts"),
                    "kernel.lzma",
                    0x90000000,
                    0x80200000,
                    "c77daf81",
                    14_482_552,
                    "4c2d6451",
                ),
                ("mmc", "dtb", "dtbs/current/board.dtb", 0xF0000000, "4afcb20e"),
                (
                    "ymodem",
                    "initrd",
                    Path("/tmp/artifacts"),
                    "installer.cpio.gz",
                    0x83000000,
                    "d1b80054",
                ),
            ],
        )
        self.assertIn(("command", "setenv initrd_size 0xd882d", {}), events)
        self.assertFalse(any("saveenv" in str(event) for event in events))


if __name__ == "__main__":
    unittest.main()
