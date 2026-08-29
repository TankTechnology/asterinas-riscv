#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Real-loopback tests for the deterministic Megrez network fixture."""

from __future__ import annotations

import hashlib
import http.client
import socket
import unittest

from tools.riscv.megrez_network_fixture import (
    FIXTURE_PATH,
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
