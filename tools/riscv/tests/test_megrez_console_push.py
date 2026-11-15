"""Console pushes must never feed a control byte to a canonical tty.

The 2026-11-11 console loss was a `cat > file` push of a binary cpio: the
guest tty stayed canonical, 0x04 ended `cat`, and the remaining bytes became
shell input (0x1a among them). These tests pin the two guarantees that make
that impossible to repeat by accident: control-byte payloads go out as base64
text, and any push estimated past the four-minute serial budget is refused.
"""

from __future__ import annotations

import base64
import hashlib
import re
import tempfile
import unittest

from tools.riscv.board.megrez_console import (
    DEFAULT_CHAR_PAUSE,
    PUSH_PACE_MS,
    Console,
    UnsafePushError,
    push_wire_seconds,
    unsafe_push_byte,
)


def write_temp(payload: bytes) -> str:
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(payload)
        return handle.name


class RecordingConsole(Console):
    """A Console whose wire is a list instead of a FIFO and a serial port."""

    def __init__(self) -> None:
        # `_push_bytes` measures the transcript size; give it a real file.
        self.log_path = write_temp(b"")
        super().__init__(log=self.log_path, fifo="/nonexistent/cmd")
        self.lines: list[str] = []
        self.raw: list[bytes] = []
        self.commands: list[str] = []
        self.guest_reply = ""
        self._pending_marker: str | None = None

    def send_line(self, line: str) -> None:
        self.lines.append(line)

    def send_raw(self, data: bytes) -> None:
        self.raw.append(data)

    def type_line(self, text: str, char_pause: float = DEFAULT_CHAR_PAUSE) -> None:
        match = re.fullmatch(r"echo (ZZPUSH\d+)", text)
        if match:
            self._pending_marker = match.group(1)

    def read_since(self, offset: int) -> tuple[str, int]:
        if self._pending_marker is not None:
            marker = self._pending_marker
            self._pending_marker = None
            return marker + "\n", offset + len(marker) + 1
        return "", offset

    def run(
        self, command: str, timeout: float, char_pause: float = DEFAULT_CHAR_PAUSE
    ) -> tuple[str, bool]:
        self.commands.append(command)
        return self.guest_reply, True

    def wire_text(self) -> str:
        """Everything written to the guest as text, minus the EOF characters."""
        return "".join(
            chunk.decode("ascii") for chunk in self.raw if chunk != b"\x04"
        )


class PayloadClassificationTest(unittest.TestCase):
    def test_control_bytes_are_binary(self) -> None:
        for byte in (0x00, 0x04, 0x1A, 0x1B, 0x7F, 0x80, 0xFF):
            payload = b"ok" + bytes([byte]) + b"ok"
            self.assertEqual(unsafe_push_byte(payload), 2, hex(byte))

    def test_script_text_is_safe(self) -> None:
        self.assertIsNone(unsafe_push_byte(b"#!/bin/sh\n\techo hi\r\n"))

    def test_empty_payload_is_safe(self) -> None:
        self.assertIsNone(unsafe_push_byte(b""))


class PushRoutingTest(unittest.TestCase):
    def test_binary_payload_is_sent_as_base64_only(self) -> None:
        payload = b"header\x00\x04\x1a\xfftail"
        console = RecordingConsole()
        expected = hashlib.sha256(payload).hexdigest()
        console.guest_reply = f"{expected}  /root/file\n"

        self.assertTrue(
            console.push(write_temp(payload), "/root/file", PUSH_PACE_MS)
        )

        encoded = base64.encodebytes(payload)
        self.assertIn(encoded, console.raw)
        for chunk in console.raw:
            if chunk == b"\x04":
                continue
            self.assertIsNone(
                unsafe_push_byte(chunk),
                "the console wrote a control byte: " + repr(chunk[:40]),
            )
        wire = console.wire_text()
        self.assertIn("cat > /root/file.b64", wire)
        self.assertNotIn("cat > /root/file\n", wire)
        self.assertTrue(
            any(
                "base64 -d /root/file.b64 > /root/file" in command
                for command in console.commands
            ),
            console.commands,
        )

    def test_plain_text_still_uses_cat(self) -> None:
        payload = b"#!/bin/sh\necho ready\n"
        console = RecordingConsole()

        self.assertTrue(
            console.push(write_temp(payload), "/root/script", PUSH_PACE_MS)
        )

        self.assertIn(payload, console.raw)
        self.assertIn("cat > /root/script", console.wire_text())
        self.assertEqual(console.commands, [])

    def test_a_digest_mismatch_is_not_success(self) -> None:
        console = RecordingConsole()
        console.guest_reply = "0" * 64 + "  /root/file\n"

        self.assertFalse(
            console.push_base64(
                write_temp(b"\x00binary\x1a"),
                "/root/file",
                PUSH_PACE_MS,
                paces=(PUSH_PACE_MS,),
            )
        )
        self.assertEqual(len(console.commands), 1)


class SerialBudgetTest(unittest.TestCase):
    def test_a_slow_text_push_is_refused_before_it_starts(self) -> None:
        console = RecordingConsole()

        with self.assertRaises(UnsafePushError) as caught:
            console.push(write_temp(b"x" * 300), "/root/file", pace_ms=1000.0)
        self.assertIn("four minutes", str(caught.exception))
        self.assertEqual(console.raw, [])
        self.assertEqual(console.lines, [])

    def test_a_slow_binary_push_is_refused_before_it_starts(self) -> None:
        console = RecordingConsole()

        with self.assertRaises(UnsafePushError) as caught:
            console.push(write_temp(b"\x00" * 300), "/root/file", pace_ms=1000.0)
        self.assertIn("four minutes", str(caught.exception))
        self.assertEqual(console.raw, [])

    def test_wire_seconds_match_the_pace(self) -> None:
        self.assertAlmostEqual(push_wire_seconds(2000, 0.15), 0.3)


if __name__ == "__main__":
    unittest.main()
