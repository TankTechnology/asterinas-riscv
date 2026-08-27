"""Focused tests for the bounded Megrez XMODEM sender."""

import unittest
from unittest import mock

from tools.riscv import megrez_xmodem as xmodem


class MegrezXmodemTests(unittest.TestCase):
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
