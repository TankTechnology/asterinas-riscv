"""Tests for bounded QMP screendumps and strict PPM display evidence."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tools.riscv.qemu_ppm import PpmAudit, audit_ppm
import tools.riscv.qemu_qmp as qmp
from tools.riscv.qemu_qmp import capture_screendump


def ppm(width: int, height: int, pixels: bytes) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode() + pixels


class PpmAuditTests(unittest.TestCase):
    def test_accepts_registered_nonempty_image(self) -> None:
        width, height = 1280, 1024
        pixels = bytearray(width * height * 3)
        for index, color in enumerate(((1, 2, 3), (4, 5, 6), (7, 8, 9))):
            pixels[index * 3 : index * 3 + 3] = bytes(color)
        pixels[3 * 3 : 67 * 3] = b"\x01\x02\x03" * 64

        audit = audit_ppm(ppm(width, height, bytes(pixels)), expected_width=width, expected_height=height)

        self.assertEqual(audit.width, width)
        self.assertEqual(audit.height, height)
        self.assertEqual(audit.max_value, 255)
        self.assertEqual(audit.non_black_pixels, 67)
        self.assertEqual(audit.distinct_colors_lower_bound, 3)
        self.assertEqual(audit.bounding_box, (0, 0, 66, 0))
        self.assertTrue(audit.passed)

    def test_rejects_short_and_trailing_pixel_data(self) -> None:
        with self.assertRaises(ValueError):
            audit_ppm(ppm(2, 1, b"\0" * 5), expected_width=2, expected_height=1)
        with self.assertRaises(ValueError):
            audit_ppm(ppm(2, 1, b"\0" * 7), expected_width=2, expected_height=1)

    def test_black_image_does_not_pass(self) -> None:
        audit = audit_ppm(ppm(8, 8, b"\0" * (8 * 8 * 3)), expected_width=8, expected_height=8)
        self.assertEqual(audit.non_black_pixels, 0)
        self.assertIsNone(audit.bounding_box)
        self.assertFalse(audit.passed)

    def test_rejects_wrong_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            audit_ppm(ppm(2, 2, b"\0" * 12), expected_width=3, expected_height=2)

    def test_rejects_non_strict_headers(self) -> None:
        invalid_headers = (
            b"P3\n1 1\n255\n",
            b"P6\r\n1 1\r\n255\r\n",
            b"P6\n01 1\n255\n",
            b"P6\n1 01\n255\n",
            b"P6\n 1 1\n255\n",
            b"P6\n1 1 \n255\n",
            b"P6\n1 1\n0255\n",
            b"P6\n1 1\n256\n",
            b"P6\n# comment\n1 1\n255\n",
            b"P6\n1 1\n255 \n",
        )
        for header in invalid_headers:
            with self.subTest(header=header), self.assertRaises(ValueError):
                audit_ppm(header + b"\0\0\0", expected_width=1, expected_height=1)

    def test_threshold_requires_64_foreground_pixels(self) -> None:
        colors = b"\x01\0\0\x02\0\0\x03\0\0"
        for count, passed in ((63, False), (64, True)):
            with self.subTest(count=count):
                pixels = (colors * ((count + 2) // 3))[: count * 3]
                audit = audit_ppm(ppm(count, 1, pixels), expected_width=count, expected_height=1)
                self.assertEqual(audit.non_black_pixels, count)
                self.assertEqual(audit.passed, passed)

    def test_threshold_requires_three_colors_at_64_foreground_pixels(self) -> None:
        two_colors = (b"\x01\0\0\x02\0\0") * 32
        three_colors = (b"\x01\0\0\x02\0\0\x03\0\0") * 21 + b"\x01\0\0"
        for pixels, passed in ((two_colors, False), (three_colors, True)):
            with self.subTest(passed=passed):
                audit = audit_ppm(ppm(64, 1, pixels), expected_width=64, expected_height=1)
                self.assertEqual(audit.non_black_pixels, 64)
                self.assertEqual(audit.passed, passed)

    def test_color_count_and_bounding_box_are_bounded_and_exact(self) -> None:
        two_colors = b"\0\0\0\x01\0\0"
        self.assertEqual(
            audit_ppm(ppm(2, 1, two_colors), expected_width=2, expected_height=1).distinct_colors_lower_bound,
            2,
        )
        pixels = bytearray(5 * 4 * 3)
        pixels[(1 * 5 + 2) * 3 : (1 * 5 + 2) * 3 + 3] = b"\x01\0\0"
        pixels[(2 * 5 + 4) * 3 : (2 * 5 + 4) * 3 + 3] = b"\0\x02\0"
        for x in range(64):
            pixels[(3 * 5 + x % 5) * 3 : (3 * 5 + x % 5) * 3 + 3] = bytes((x % 3 + 1, 0, 0))
        audit = audit_ppm(ppm(5, 4, bytes(pixels)), expected_width=5, expected_height=4)
        self.assertEqual(audit.distinct_colors_lower_bound, 3)
        self.assertEqual(audit.bounding_box, (0, 1, 4, 3))


class QmpCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.outside_tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.outside_root = Path(self.outside_tempdir.name)
        self.root.chmod(0o700)
        self.socket_path = self.root / "qmp.sock"
        self.output_path = self.root / "screen.ppm"
        self.threads: list[threading.Thread] = []
        self.listeners: list[socket.socket] = []
        self.server_errors: list[BaseException] = []

    def tearDown(self) -> None:
        try:
            for listener in self.listeners:
                listener.close()
            for thread in self.threads:
                thread.join(2)
                self.assertFalse(thread.is_alive(), "fake QMP server did not finish")
            if self.server_errors:
                raise self.server_errors[0]
        finally:
            self.tempdir.cleanup()
            self.outside_tempdir.cleanup()

    def start_server(self, handler) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(self.socket_path))
        listener.listen(1)
        listener.settimeout(2)
        self.listeners.append(listener)

        def serve() -> None:
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    handler(connection)
            except BaseException as error:  # Propagate assertions to the test thread.
                self.server_errors.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=serve)
        thread.start()
        self.threads.append(thread)

    @staticmethod
    def receive_line(connection: socket.socket) -> bytes:
        data = bytearray()
        while not data.endswith(b"\n"):
            part = connection.recv(1)
            if not part:
                raise AssertionError("client closed before newline")
            data.extend(part)
        return bytes(data)

    def join_last_server(self) -> None:
        thread = self.threads.pop()
        thread.join(2)
        self.assertFalse(thread.is_alive(), "fake QMP server did not finish")
        if self.server_errors:
            raise self.server_errors.pop(0)

    def test_fixed_two_command_protocol_returns_exact_output(self) -> None:
        payload = ppm(1, 1, b"\x01\x02\x03")

        def handler(connection: socket.socket) -> None:
            connection.sendall(b'{"QMP":{"version":{}}}\n')
            self.assertEqual(self.receive_line(connection), b'{"execute":"qmp_capabilities"}\n')
            connection.sendall(b'{"return":{}}\n')
            expected = json.dumps(
                {"execute": "screendump", "arguments": {"filename": os.fspath(self.output_path)}},
                separators=(",", ":"),
            ).encode() + b"\n"
            self.assertEqual(self.receive_line(connection), expected)
            self.output_path.write_bytes(payload)
            connection.sendall(b'{"return":{}}\n')
            self.assertEqual(connection.recv(1), b"")

        self.start_server(handler)
        self.assertEqual(capture_screendump(self.socket_path, self.output_path, capture_root=self.root), payload)

    def test_rejects_bad_greeting_and_command_responses(self) -> None:
        cases = (
            (b"not json\n", None),
            (b"{}\n", None),
            (b"[]\n", None),
            (b'"greeting"\n', None),
            (b'{"QMP":{}}\n', b'{"error":{}}\n'),
            (b'{"QMP":{}}\n', b'{"event":"STOP"}\n'),
            (b'{"QMP":{}}\n', b"{}\n"),
        )
        for case in cases:
            with self.subTest(case=case):
                self.socket_path.unlink(missing_ok=True)
                greeting = case[0]
                response = case[1]

                def handler(connection: socket.socket, greeting=greeting, response=response) -> None:
                    connection.sendall(greeting)
                    if response is not None:
                        self.receive_line(connection)
                        connection.sendall(response)

                self.start_server(handler)
                with self.assertRaises(ValueError):
                    capture_screendump(self.socket_path, self.output_path, capture_root=self.root)
                self.join_last_server()

    def test_rejects_nonstandard_or_invalid_screendump_responses(self) -> None:
        for response in (b'{"return":NaN}\n', b'{"return":Infinity}\n', b'{"return":-Infinity}\n', b"[]\n", b'"reply"\n', b'{"error":{}}\n', b'{"event":"STOP"}\n', b"{}\n"):
            with self.subTest(response=response):
                self.socket_path.unlink(missing_ok=True)

                def handler(connection: socket.socket, response=response) -> None:
                    connection.sendall(b'{"QMP":{}}\n')
                    self.assertEqual(self.receive_line(connection), b'{"execute":"qmp_capabilities"}\n')
                    connection.sendall(b'{"return":{}}\n')
                    self.receive_line(connection)
                    self.output_path.write_bytes(ppm(1, 1, b"\x01\x02\x03"))
                    connection.sendall(response)

                self.start_server(handler)
                with self.assertRaises(ValueError):
                    capture_screendump(self.socket_path, self.output_path, capture_root=self.root)
                self.join_last_server()

    def test_rejects_nonstandard_json_constants_in_greeting(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                self.socket_path.unlink(missing_ok=True)

                def handler(connection: socket.socket, constant=constant) -> None:
                    connection.sendall(b'{"QMP":' + constant + b"}\n")
                    first_command = connection.recv(1024)
                    if not first_command:
                        return
                    self.assertEqual(first_command, b'{"execute":"qmp_capabilities"}\n')
                    connection.sendall(b'{"return":{}}\n')
                    self.receive_line(connection)
                    self.output_path.write_bytes(ppm(1, 1, b"\x01\x02\x03"))
                    connection.sendall(b'{"return":{}}\n')

                self.start_server(handler)
                with self.assertRaises(ValueError):
                    capture_screendump(self.socket_path, self.output_path, capture_root=self.root)
                self.join_last_server()

    def test_reports_distinct_json_decode_failures(self) -> None:
        cases = (
            (b"\xff\n", "not UTF-8"),
            (b'{"QMP":}\n', "invalid JSON"),
            (b'{"QMP":NaN}\n', "forbidden JSON constant"),
        )
        for greeting, message in cases:
            with self.subTest(greeting=greeting):
                self.socket_path.unlink(missing_ok=True)

                def handler(connection: socket.socket, greeting=greeting) -> None:
                    connection.sendall(greeting)

                self.start_server(handler)
                with self.assertRaisesRegex(ValueError, message):
                    capture_screendump(self.socket_path, self.output_path, capture_root=self.root)
                self.join_last_server()

    def test_rejects_eof_and_overlong_response(self) -> None:
        for response in (b"", b"{" + b"x" * (64 * 1024) + b"\n"):
            with self.subTest(response_length=len(response)):
                self.socket_path.unlink(missing_ok=True)

                def handler(connection: socket.socket, response=response) -> None:
                    connection.sendall(b'{"QMP":{}}\n')
                    self.receive_line(connection)
                    connection.sendall(response)

                self.start_server(handler)
                with self.assertRaises(ValueError):
                    capture_screendump(self.socket_path, self.output_path, capture_root=self.root)
                self.join_last_server()

    def test_server_may_unlink_socket_after_accept(self) -> None:
        payload = ppm(1, 1, b"\x01\x02\x03")

        def handler(connection: socket.socket) -> None:
            self.socket_path.unlink()
            connection.sendall(b'{"QMP":{}}\n')
            self.assertEqual(self.receive_line(connection), b'{"execute":"qmp_capabilities"}\n')
            connection.sendall(b'{"return":{}}\n')
            self.receive_line(connection)
            self.output_path.write_bytes(payload)
            connection.sendall(b'{"return":{}}\n')

        self.start_server(handler)
        self.assertEqual(capture_screendump(self.socket_path, self.output_path, capture_root=self.root), payload)

    def test_safe_reader_enforces_the_registered_capture_limit(self) -> None:
        directory = self.root / "output"
        directory.mkdir()
        exact = directory / "exact"
        exact.write_bytes(b"x" * qmp._MAX_CAPTURE_BYTES)
        oversized = directory / "oversized"
        with oversized.open("wb") as output:
            output.truncate(qmp._MAX_CAPTURE_BYTES + 1024 * 1024)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        try:
            self.assertEqual(qmp._read_output(descriptor, "exact"), b"x" * qmp._MAX_CAPTURE_BYTES)
            with self.assertRaises(ValueError):
                qmp._read_output(descriptor, "oversized")
        finally:
            os.close(descriptor)

    def test_safe_reader_collects_short_reads_and_rejects_overflow_across_chunks(self) -> None:
        directory = self.root / "output"
        directory.mkdir()
        (directory / "screen").write_bytes(b"placeholder")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(qmp.os, "read", side_effect=(b"first", b"-second", b"")) as read:
                self.assertEqual(qmp._read_output(descriptor, "screen"), b"first-second")
                self.assertEqual(
                    [call.args[1] for call in read.call_args_list],
                    [qmp._MAX_CAPTURE_BYTES + 1, qmp._MAX_CAPTURE_BYTES - 4, qmp._MAX_CAPTURE_BYTES - 11],
                )
            with mock.patch.object(
                qmp.os,
                "read",
                side_effect=(b"x" * qmp._MAX_CAPTURE_BYTES, b"y", b""),
            ) as read:
                with self.assertRaises(ValueError):
                    qmp._read_output(descriptor, "screen")
                self.assertEqual([call.args[1] for call in read.call_args_list], [qmp._MAX_CAPTURE_BYTES + 1, 1])
        finally:
            os.close(descriptor)

    def test_qmp_module_imports_as_a_top_level_riscv_runner_module(self) -> None:
        riscv_tools = Path(__file__).parents[1]
        environment = {"PYTHONPATH": os.fspath(riscv_tools)}
        result = subprocess.run(
            [sys.executable, "-c", "import qemu_qmp"],
            cwd="/tmp",
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_total_deadline_rejects_a_trickling_greeting(self) -> None:
        def handler(connection: socket.socket) -> None:
            try:
                for byte in b'{"QMP":{}}\n':
                    connection.sendall(bytes((byte,)))
                    threading.Event().wait(0.01)
            except BrokenPipeError:
                pass

        self.start_server(handler)
        with self.assertRaisesRegex(TimeoutError, "QMP capture timed out"):
            capture_screendump(self.socket_path, self.output_path, capture_root=self.root, timeout=0.05)

    def test_capture_uses_retained_parent_descriptor_after_path_replacement(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        output_path = nested / "screen.ppm"
        expected = ppm(1, 1, b"\x01\x02\x03")
        outside_payload = ppm(1, 1, b"\x09\x08\x07")
        (self.outside_root / "screen.ppm").write_bytes(outside_payload)

        def handler(connection: socket.socket) -> None:
            connection.sendall(b'{"QMP":{}}\n')
            self.receive_line(connection)
            connection.sendall(b'{"return":{}}\n')
            self.receive_line(connection)
            output_path.write_bytes(expected)
            nested.rename(self.outside_root / "moved")
            nested.symlink_to(self.outside_root, target_is_directory=True)
            connection.sendall(b'{"return":{}}\n')

        self.start_server(handler)
        self.assertEqual(capture_screendump(self.socket_path, output_path, capture_root=self.root), expected)

    def test_validates_timeout_and_path_safety_before_connecting(self) -> None:
        for timeout in (0, -1, math.inf, math.nan):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                capture_screendump(self.socket_path, self.output_path, capture_root=self.root, timeout=timeout)
        with self.assertRaises(ValueError):
            capture_screendump(Path("relative"), self.output_path, capture_root=self.root)
        with self.assertRaises(ValueError):
            capture_screendump(self.socket_path, self.root, capture_root=self.root)
        comma_socket = self.root / "bad,socket"
        with self.assertRaises(ValueError):
            capture_screendump(comma_socket, self.output_path, capture_root=self.root)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            capture_screendump(self.socket_path, self.output_path, capture_root=self.root)

    def test_rejects_symlinks_and_nonregular_or_missing_output(self) -> None:
        outside = self.outside_root
        link = self.root / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            capture_screendump(link / "qmp.sock", self.output_path, capture_root=self.root)
        socket_link = self.root / "socket-link"
        socket_link.symlink_to(outside / "qmp.sock")
        with self.assertRaises(ValueError):
            capture_screendump(socket_link, self.output_path, capture_root=self.root)
        outside_output = outside / "screen.ppm"
        outside_output.write_bytes(b"outside")
        output_link = self.root / "output-link"
        output_link.symlink_to(outside_output)
        with self.assertRaises(ValueError):
            capture_screendump(self.socket_path, output_link, capture_root=self.root)
        self.output_path.mkdir()
        with self.assertRaises(ValueError):
            capture_screendump(self.socket_path, self.output_path, capture_root=self.root)
        self.output_path.rmdir()

        def handler(connection: socket.socket) -> None:
            connection.sendall(b'{"QMP":{}}\n')
            self.receive_line(connection)
            connection.sendall(b'{"return":{}}\n')
            self.receive_line(connection)
            connection.sendall(b'{"return":{}}\n')

        self.start_server(handler)
        with self.assertRaises(ValueError):
            capture_screendump(self.socket_path, self.output_path, capture_root=self.root)


if __name__ == "__main__":
    unittest.main()
