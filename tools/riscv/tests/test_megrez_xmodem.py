"""Focused tests for the bounded Megrez XMODEM sender."""

import os
import pty
import select
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv import megrez_xmodem as xmodem


class MegrezXmodemTests(unittest.TestCase):
    def _run_pty_transfer(
        self, *, current_baud: int, descriptor_api: bool = False
    ) -> None:
        payload = (b"Asterinas-XMODEM-PTY-" * 70) + b"done"
        address = 0x83000000
        errors: list[BaseException] = []
        received = bytearray()
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        deadline = time.monotonic() + 5.0

        def read_exact(length: int) -> bytes:
            data = bytearray()
            while len(data) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"PTY peer timed out after {len(data)}/{length}")
                readable, _, _ = select.select([master_fd], [], [], remaining)
                if not readable:
                    raise TimeoutError("PTY peer read deadline expired")
                chunk = os.read(master_fd, length - len(data))
                if not chunk:
                    raise EOFError("PTY peer observed EOF")
                data.extend(chunk)
            return bytes(data)

        def read_until(marker: bytes) -> bytes:
            data = bytearray()
            while marker not in data:
                data.extend(read_exact(1))
                if len(data) > 4096:
                    raise AssertionError("PTY command exceeds 4 KiB")
            return bytes(data)

        def peer() -> None:
            try:
                expected_command = (
                    f"loadx 0x{address:x} {xmodem.TRANSFER_BAUD}\r".encode()
                    if current_baud == xmodem.INITIAL_BAUD
                    else f"loadx 0x{address:x}\r".encode()
                )
                self.assertEqual(read_until(b"\r"), expected_command)
                if current_baud == xmodem.INITIAL_BAUD:
                    os.write(
                        master_fd,
                        b"## Switch baudrate to 1500000 bps and press ENTER ...",
                    )
                    self.assertEqual(read_exact(1), b"\r")
                os.write(master_fd, bytes((xmodem.CRC_REQUEST,)))

                expected_blocks = (
                    len(payload) + xmodem.BLOCK_SIZE - 1
                ) // xmodem.BLOCK_SIZE
                for index in range(expected_blocks):
                    packet = read_exact(3 + xmodem.BLOCK_SIZE + 2)
                    self.assertEqual(packet[0], xmodem.STX)
                    self.assertEqual(packet[1], (index + 1) & 0xFF)
                    self.assertEqual(packet[2], 0xFF - packet[1])
                    block = packet[3 : 3 + xmodem.BLOCK_SIZE]
                    self.assertEqual(
                        int.from_bytes(packet[-2:], "big"),
                        xmodem.crc16_xmodem(block),
                    )
                    received.extend(block)
                    os.write(master_fd, bytes((xmodem.ACK,)))

                self.assertEqual(read_exact(1), bytes((xmodem.EOT,)))
                os.write(master_fd, bytes((xmodem.ACK,)))
                completion = (
                    f"\r\nTotal Size = 0x{len(payload):08x} = {len(payload)} Bytes\r\n"
                    f"Start Addr = 0x{address:x}\r\n"
                ).encode()
                if current_baud == xmodem.INITIAL_BAUD:
                    os.write(master_fd, completion + b"press ESC")
                    sent_at = time.monotonic()
                    self.assertEqual(read_exact(1), bytes((0x1B,)))
                    self.assertGreaterEqual(time.monotonic() - sent_at, 0.005)
                    os.write(master_fd, b"\r\n=> ")
                else:
                    os.write(master_fd, completion + b"=> ")
                    readable, _, _ = select.select([master_fd], [], [], 0.05)
                    self.assertFalse(
                        readable, "same-baud completion sent an unexpected ESC"
                    )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(xmodem, "BAUD_SETTLE_SECONDS", 0.01),
            ):
                artifact = Path(directory) / "artifact"
                artifact.write_bytes(payload)
                if descriptor_api:
                    result = xmodem.transfer_fd(
                        slave_fd,
                        artifact,
                        address,
                        current_baud=current_baud,
                    )
                    os.fstat(slave_fd)
                else:
                    result = xmodem.transfer(
                        slave_name,
                        artifact,
                        address,
                        current_baud=current_baud,
                    )
            self.assertEqual(
                result.blocks,
                (len(payload) + xmodem.BLOCK_SIZE - 1) // xmodem.BLOCK_SIZE,
            )
            self.assertEqual(result.retries, 0)
            self.assertEqual(received[: len(payload)], payload)
            self.assertEqual(
                received[len(payload) :],
                bytes((xmodem.PAD,)) * (len(received) - len(payload)),
            )
            attributes = termios.tcgetattr(slave_fd)
            expected_speed = (
                termios.B115200
                if current_baud == xmodem.INITIAL_BAUD
                else termios.B1500000
            )
            self.assertEqual(attributes[4], expected_speed)
            self.assertEqual(attributes[5], expected_speed)
        finally:
            thread.join(timeout=5)
            os.close(master_fd)
            os.close(slave_fd)
        self.assertFalse(thread.is_alive(), "PTY peer thread did not terminate")
        if errors:
            raise errors[0]

    def test_pty_initial_baud_transfer(self) -> None:
        self._run_pty_transfer(current_baud=xmodem.INITIAL_BAUD)

    def test_pty_existing_transfer_baud(self) -> None:
        self._run_pty_transfer(current_baud=xmodem.TRANSFER_BAUD)

    def test_pty_descriptor_transfer_keeps_owned_fd_open(self) -> None:
        self._run_pty_transfer(
            current_baud=xmodem.INITIAL_BAUD,
            descriptor_api=True,
        )

    def test_crc_and_one_kibibyte_packet_match_xmodem(self) -> None:
        self.assertEqual(xmodem.crc16_xmodem(b"123456789"), 0x31C3)

        packet = xmodem.build_packet(1, b"abc")

        self.assertEqual(packet[0], xmodem.STX)
        self.assertEqual(packet[1:3], bytes((1, 0xFE)))
        self.assertEqual(packet[3:6], b"abc")
        self.assertEqual(packet[6 : 3 + xmodem.BLOCK_SIZE], b"\x1a" * 1021)
        self.assertEqual(
            int.from_bytes(packet[-2:], "big"),
            xmodem.crc16_xmodem(packet[3:-2]),
        )
        self.assertEqual(xmodem.build_packet(0, b"x")[1:3], b"\x00\xff")

    def test_sender_retries_nak_and_finishes_with_acknowledged_eot(self) -> None:
        controls = iter((xmodem.CRC_REQUEST, xmodem.NAK, xmodem.ACK, xmodem.ACK))
        writes: list[bytes] = []

        result = xmodem.send_payload(
            b"payload",
            read_control=lambda _timeout: next(controls),
            write_all=writes.append,
            response_timeout=1.0,
            retry_limit=2,
        )

        self.assertEqual(result.blocks, 1)
        self.assertEqual(result.retries, 1)
        self.assertEqual(writes[0], writes[1])
        self.assertEqual(writes[-1], bytes((xmodem.EOT,)))

    def test_sender_rejects_cancel_timeout_and_exhausted_retries(self) -> None:
        for controls, message in (
            ((xmodem.CAN,), "cancelled"),
            ((None,), "start handshake"),
            ((xmodem.CRC_REQUEST, xmodem.NAK, xmodem.NAK), "retry limit"),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(xmodem.TransferError, message),
            ):
                values = iter(controls)
                xmodem.send_payload(
                    b"x",
                    read_control=lambda _timeout: next(values, None),
                    write_all=mock.Mock(),
                    response_timeout=1.0,
                    retry_limit=1,
                )

    def test_cli_rejects_invalid_input_before_opening_serial(self) -> None:
        with (
            mock.patch.object(xmodem.os, "open") as open_serial,
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(xmodem.main(["serial", "missing", "0x90000000"]), 2)
        open_serial.assert_not_called()

    def test_cli_accepts_the_board_already_at_transfer_baud(self) -> None:
        values = xmodem._parser().parse_args(
            [
                "--current-baud",
                str(xmodem.TRANSFER_BAUD),
                "serial",
                "artifact",
                "0x83000000",
            ]
        )

        self.assertEqual(values.current_baud, xmodem.TRANSFER_BAUD)

    def test_enter_stabilizes_new_baud_before_carriage_return(self) -> None:
        calls: list[tuple[str, object]] = []
        with (
            mock.patch.object(
                xmodem,
                "_write_all",
                side_effect=lambda _fd, data: calls.append(("write", data)),
            ),
            mock.patch.object(
                xmodem,
                "_read_until",
                side_effect=lambda _fd, marker, _timeout: calls.append(
                    ("wait", marker)
                ),
            ),
            mock.patch.object(
                xmodem,
                "_read_control",
                side_effect=lambda _fd, _timeout: (
                    calls.append(("control", xmodem.CRC_REQUEST)) or xmodem.CRC_REQUEST
                ),
            ),
            mock.patch.object(
                xmodem,
                "_configure_serial",
                side_effect=lambda _fd, baud: calls.append(("baud", baud)),
            ),
            mock.patch.object(
                xmodem.time,
                "sleep",
                side_effect=lambda seconds: calls.append(("sleep", seconds)),
            ),
        ):
            xmodem._enter_transfer_mode(7, 0x90000000)

        self.assertEqual(
            calls,
            [
                ("write", b"loadx 0x90000000 1500000\r"),
                ("wait", b"press ENTER"),
                ("baud", xmodem.TRANSFER_BAUD),
                ("sleep", xmodem.BAUD_SETTLE_SECONDS),
                ("write", b"\r"),
                ("control", xmodem.CRC_REQUEST),
            ],
        )

    def test_enter_reuses_existing_transfer_baud_without_switch_handshake(self) -> None:
        calls: list[tuple[str, object]] = []
        with (
            mock.patch.object(
                xmodem,
                "_write_all",
                side_effect=lambda _fd, data: calls.append(("write", data)),
            ),
            mock.patch.object(
                xmodem,
                "_read_control",
                side_effect=lambda _fd, _timeout: (
                    calls.append(("control", xmodem.CRC_REQUEST)) or xmodem.CRC_REQUEST
                ),
            ),
            mock.patch.object(
                xmodem,
                "_configure_serial",
                side_effect=lambda _fd, baud: calls.append(("baud", baud)),
            ),
        ):
            xmodem._enter_transfer_mode(
                7, 0x83000000, current_baud=xmodem.TRANSFER_BAUD
            )

        self.assertEqual(
            calls,
            [
                ("write", b"loadx 0x83000000\r"),
                ("control", xmodem.CRC_REQUEST),
            ],
        )

    def test_leave_transfer_mode_sends_escape_before_waiting_for_real_prompt(
        self,
    ) -> None:
        calls: list[tuple[str, object]] = []
        with (
            mock.patch.object(
                xmodem,
                "_write_all",
                side_effect=lambda _fd, data: calls.append(("write", data)),
            ),
            mock.patch.object(
                xmodem,
                "_read_prompt",
                side_effect=lambda _fd, _timeout: calls.append(("prompt", None)),
            ),
            mock.patch.object(
                xmodem,
                "_configure_serial",
                side_effect=lambda _fd, baud: calls.append(("baud", baud)),
            ),
            mock.patch.object(
                xmodem.time,
                "sleep",
                side_effect=lambda seconds: calls.append(("sleep", seconds)),
            ),
        ):
            xmodem._leave_transfer_mode(7)

        self.assertEqual(
            calls,
            [
                ("baud", xmodem.INITIAL_BAUD),
                ("sleep", xmodem.BAUD_SETTLE_SECONDS),
                ("write", bytes((0x1B,))),
                ("prompt", None),
            ],
        )

    def test_same_baud_transfer_finishes_at_prompt_without_escape(self) -> None:
        transcript = (
            b"Total Size      = 0x00000800 = 2048 Bytes\r\n"
            b"Start Addr      = 0x83000000\r\n=> "
        )
        with (
            mock.patch.object(xmodem, "_read_prompt", return_value=transcript),
            mock.patch.object(xmodem, "_read_until") as read_until,
            mock.patch.object(xmodem, "_leave_transfer_mode") as leave,
        ):
            xmodem._finish_transfer(
                7,
                payload_size=2048,
                address=0x83000000,
                current_baud=xmodem.TRANSFER_BAUD,
            )

        read_until.assert_not_called()
        leave.assert_not_called()


if __name__ == "__main__":
    unittest.main()
