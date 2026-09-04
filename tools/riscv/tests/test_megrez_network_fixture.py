#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Real-loopback tests for the deterministic Megrez network fixture."""

from __future__ import annotations

import hashlib
import http.client
import socket
import unittest

from tools.riscv.megrez_network_fixture import (
    BROWSER_API,
    BROWSER_API_PATH,
    BROWSER_AUDIO,
    BROWSER_AUDIO_PATH,
    BROWSER_CAPTURE_PATH,
    BROWSER_PNG_CAPTURE_PATH,
    BROWSER_DOWNLOAD,
    BROWSER_DOWNLOAD_PATH,
    BROWSER_IMAGE,
    BROWSER_IMAGE_PATH,
    BROWSER_INDEX,
    BROWSER_INDEX_PATH,
    BROWSER_SEARCH,
    BROWSER_SECOND,
    BROWSER_SECOND_PATH,
    FIXTURE_PATH,
    MAX_CAPTURE_BYTES,
    MAX_REQUEST_RECORDS,
    PAYLOAD,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
    _parse_args,
    is_successful_summary,
)


class MegrezNetworkFixtureTests(unittest.TestCase):
    def request(
        self, server: FixtureServer, path: str = FIXTURE_PATH
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(
            server.address,
            server.port,
            timeout=1,
        )
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, body, headers
        finally:
            connection.close()

    def post(
        self,
        server: FixtureServer,
        path: str,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(server.address, server.port, timeout=1)
        try:
            connection.request("POST", path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def raw_request(self, server: FixtureServer, request: bytes) -> bytes:
        with socket.create_connection((server.address, server.port), timeout=1) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while chunk := sock.recv(4096):
                chunks.append(chunk)
        return b"".join(chunks)

    def test_serves_one_exact_payload_and_canonical_summary(self) -> None:
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            status, body, headers = self.request(server)
            missing_status, missing_body, _ = self.request(server, "/missing")
            summary = server.summary()

        self.assertEqual(status, 200)
        self.assertEqual(len(body), PAYLOAD_SIZE)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(hashlib.sha256(body).hexdigest(), PAYLOAD_SHA256)
        self.assertEqual(headers["content-length"], str(PAYLOAD_SIZE))
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_body, b"")
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["payload_sha256"], PAYLOAD_SHA256)
        self.assertEqual(summary["payload_size"], PAYLOAD_SIZE)
        self.assertEqual(
            [record["status"] for record in summary["requests"]],
            [200, 404],
        )
        self.assertFalse(is_successful_summary(summary, expected_requests=2))

        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as complete:
            for _ in range(20):
                self.assertEqual(self.request(complete)[0], 200)
            complete_summary = complete.summary()
        self.assertTrue(is_successful_summary(complete_summary, expected_requests=20))

    def test_peer_allowlist_and_request_record_cap(self) -> None:
        with FixtureServer(
            FixtureConfig("127.0.0.1", 0, allowed_peer="192.0.2.1")
        ) as denied:
            status, body, _ = self.request(denied)
            denied_summary = denied.summary()
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")
        self.assertEqual(denied_summary["requests"][0]["peer"], "127.0.0.1")

        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            for _ in range(MAX_REQUEST_RECORDS + 3):
                self.assertEqual(self.request(server)[0], 200)
            summary = server.summary()
        self.assertEqual(summary["request_count"], MAX_REQUEST_RECORDS + 3)
        self.assertEqual(len(summary["requests"]), MAX_REQUEST_RECORDS)
        self.assertTrue(summary["records_truncated"])
        timestamps = [record["monotonic_ns"] for record in summary["requests"]]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_serves_deterministic_browser_resources_without_legacy_records(
        self,
    ) -> None:
        expected = {
            BROWSER_INDEX_PATH: ("text/html; charset=utf-8", BROWSER_INDEX),
            BROWSER_SECOND_PATH: ("text/html; charset=utf-8", BROWSER_SECOND),
            BROWSER_IMAGE_PATH: ("image/png", BROWSER_IMAGE),
            BROWSER_DOWNLOAD_PATH: (
                "application/octet-stream",
                BROWSER_DOWNLOAD,
            ),
            BROWSER_API_PATH: ("application/json", BROWSER_API),
            BROWSER_AUDIO_PATH: ("audio/wav", BROWSER_AUDIO),
        }
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            for path, (content_type, expected_body) in expected.items():
                with self.subTest(path=path):
                    status, body, headers = self.request(server, path)
                    self.assertEqual(status, 200)
                    self.assertEqual(body, expected_body)
                    self.assertEqual(headers["content-type"], content_type)
                    self.assertEqual(headers["content-length"], str(len(body)))
                    self.assertEqual(headers["cache-control"], "no-store")

            status, body, _ = self.request(server, f"{BROWSER_INDEX_PATH}?q=asterinas")
            self.assertEqual(status, 200)
            self.assertEqual(body, BROWSER_SEARCH)
            for invalid_query in ("?q=asterinas&q=again", "?other=asterinas", "?q="):
                with self.subTest(query=invalid_query):
                    self.assertEqual(
                        self.request(server, BROWSER_INDEX_PATH + invalid_query)[0],
                        400,
                    )
            self.assertEqual(server.summary()["request_count"], 0)

    def test_browser_wasm_probe_is_bounded(self) -> None:
        source = BROWSER_INDEX.decode("utf-8")
        self.assertIn(
            "await failAfter(WebAssembly.instantiate(new Uint8Array(", source
        )
        self.assertIn(")), 'wasm')).instance.exports.answer()", source)
        self.assertIn("get('capabilities') === '1'", source)
        self.assertIn("checks.worker = typeof Worker === 'function'", source)

    def test_accepts_one_bounded_capture_and_reports_immutable_evidence(self) -> None:
        payload = b"xwd-capture"
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            status, body = self.post(server, BROWSER_CAPTURE_PATH, payload)
            self.assertEqual(status, 201)
            self.assertEqual(body, b"")
            self.assertEqual(server.capture_payload(), payload)
            self.assertEqual(
                server.capture_summary(),
                {
                    "bytes": len(payload),
                    "path": BROWSER_CAPTURE_PATH,
                    "peer": "127.0.0.1",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            )
            self.assertEqual(server.summary()["request_count"], 0)
            self.assertEqual(
                self.post(server, BROWSER_CAPTURE_PATH, b"second")[0],
                409,
            )
            self.assertEqual(server.capture_payload(), payload)

    def test_accepts_firefox_png_capture_endpoint(self) -> None:
        payload = BROWSER_IMAGE
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            status, body = self.post(server, BROWSER_PNG_CAPTURE_PATH, payload)
            self.assertEqual((status, body), (201, b""))
            self.assertEqual(server.capture_payload(), payload)
            self.assertEqual(server.capture_summary()["path"], BROWSER_PNG_CAPTURE_PATH)

    def test_rejects_invalid_capture_boundaries_without_state(self) -> None:
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            cases = (
                ({"Content-Length": "0"}, b"", 400),
                ({"Content-Length": "abc"}, b"x", 400),
                ({"Content-Length": str(MAX_CAPTURE_BYTES + 1)}, b"", 413),
                ({"Transfer-Encoding": "chunked"}, b"0\r\n\r\n", 400),
            )
            for headers, body, expected_status in cases:
                with self.subTest(headers=headers):
                    status, _ = self.post(
                        server,
                        BROWSER_CAPTURE_PATH,
                        body,
                        headers,
                    )
                    self.assertEqual(status, expected_status)
                    self.assertIsNone(server.capture_payload())

            missing_length = self.raw_request(
                server,
                b"POST /browser-quality/capture.xwd.gz HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n",
            )
            self.assertIn(b" 411 ", missing_length.split(b"\r\n", 1)[0])
            short_body = self.raw_request(
                server,
                b"POST /browser-quality/capture.xwd.gz HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nContent-Length: 10\r\n"
                b"Connection: close\r\n\r\nabc",
            )
            self.assertIn(b" 400 ", short_body.split(b"\r\n", 1)[0])
            self.assertIsNone(server.capture_payload())

        with FixtureServer(
            FixtureConfig("127.0.0.1", 0, allowed_peer="192.0.2.1")
        ) as denied:
            self.assertEqual(
                self.post(denied, BROWSER_CAPTURE_PATH, b"capture")[0],
                403,
            )
            self.assertIsNone(denied.capture_payload())

    def test_cleanup_is_idempotent_and_port_conflict_fails_before_thread(self) -> None:
        server = FixtureServer(FixtureConfig("127.0.0.1", 0))
        server.start()
        thread = server.thread
        server.close()
        server.close()
        self.assertFalse(server.running)
        self.assertFalse(thread.is_alive())

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        conflict = FixtureServer(FixtureConfig("127.0.0.1", listener.getsockname()[1]))
        with self.assertRaises(OSError):
            conflict.start()
        self.assertFalse(conflict.running)

    def test_configuration_and_cli_reject_invalid_network_values(self) -> None:
        for config in (
            lambda: FixtureConfig("not-an-address", 17894),
            lambda: FixtureConfig("127.0.0.1", True),
            lambda: FixtureConfig("127.0.0.1", -1),
            lambda: FixtureConfig("127.0.0.1", 65536),
            lambda: FixtureConfig("127.0.0.1", 17894, allowed_peer="bad"),
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    config()

        parsed = _parse_args(
            [
                "--bind-address",
                "127.0.0.1",
                "--port",
                "17894",
                "--allow-peer",
                "127.0.0.1",
            ]
        )
        self.assertEqual(parsed, FixtureConfig("127.0.0.1", 17894, "127.0.0.1"))
        for arguments in (
            ["--bind-address", "bad"],
            ["--port", "-1"],
            ["--port", "70000"],
            ["--allow-peer", "bad"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    _parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
