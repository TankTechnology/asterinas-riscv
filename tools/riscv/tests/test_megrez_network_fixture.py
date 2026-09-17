#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Real-loopback tests for the deterministic Megrez network fixture."""

from __future__ import annotations

import hashlib
import http.client
import shutil
import socket
import subprocess
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
    BROWSER_PERF,
    BROWSER_PERF_PATH,
    BROWSER_PERF_SECOND,
    BROWSER_PERF_SECOND_PATH,
    BROWSER_SEARCH,
    BROWSER_SECOND,
    BROWSER_SECOND_PATH,
    BROWSER_WORKLOAD,
    BROWSER_WORKLOAD_IMAGE_PATH,
    BROWSER_WORKLOAD_PATH,
    BROWSER_WORKLOAD_RESOURCE_PATH,
    FIXTURE_PATH,
    MAX_CAPTURE_BYTES,
    MAX_REQUEST_RECORDS,
    PAYLOAD,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    WORKLOAD_RESOURCE,
    WORKLOAD_RESOURCE_SIZE,
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

    def test_serves_isolated_performance_pages_without_capability_work(self) -> None:
        expected = {
            BROWSER_PERF_PATH: BROWSER_PERF,
            BROWSER_PERF_SECOND_PATH: BROWSER_PERF_SECOND,
        }
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            for path, expected_body in expected.items():
                with self.subTest(path=path):
                    status, body, headers = self.request(server, path)
                    self.assertEqual(status, 200)
                    self.assertEqual(body, expected_body)
                    self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
                    self.assertEqual(self.request(server, path + "?q=x")[0], 400)
            self.assertEqual(server.summary()["request_count"], 0)

        self.assertIn(b"__asterinasTimingSnapshot", BROWSER_PERF)
        self.assertIn(b"requestAnimationFrame", BROWSER_PERF)
        self.assertIn(b"event.isTrusted", BROWSER_PERF)
        self.assertIn(b"keyboard", BROWSER_PERF)
        self.assertIn(b"pointer", BROWSER_PERF)
        self.assertIn(b"scroll", BROWSER_PERF)
        self.assertIn(BROWSER_PERF_SECOND_PATH.encode(), BROWSER_PERF)
        self.assertIn(b"getEntriesByType('navigation')", BROWSER_PERF_SECOND)
        for contaminant in (b"indexedDB", b"WebAssembly", b"__asterinasCapabilities"):
            self.assertNotIn(contaminant, BROWSER_PERF)

    def test_serves_composite_workload_and_bounded_resources(self) -> None:
        query = "mode=smoke&phase=resource&sequence=7&pass=cold"
        image_query = "mode=profile&phase=image&sequence=3&pass=warm"
        with FixtureServer(FixtureConfig("127.0.0.1", 0)) as server:
            status, page, headers = self.request(server, BROWSER_WORKLOAD_PATH)
            resource_status, resource, resource_headers = self.request(
                server, f"{BROWSER_WORKLOAD_RESOURCE_PATH}?{query}"
            )
            image_status, image, image_headers = self.request(
                server, f"{BROWSER_WORKLOAD_IMAGE_PATH}?{image_query}"
            )
            for invalid in (
                "",
                "phase=resource&mode=smoke&sequence=7&pass=cold",
                "mode=smoke&phase=unknown&sequence=7&pass=cold",
                "mode=smoke&phase=resource&sequence=256&pass=cold",
                "mode=smoke&phase=resource&sequence=7&pass=cold&extra=1",
            ):
                separator = "?" if invalid else ""
                self.assertEqual(
                    self.request(
                        server, BROWSER_WORKLOAD_RESOURCE_PATH + separator + invalid
                    )[0],
                    400,
                )
            summary = server.summary()

        self.assertEqual((status, page), (200, BROWSER_WORKLOAD))
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual((resource_status, resource), (200, WORKLOAD_RESOURCE))
        self.assertEqual(len(resource), WORKLOAD_RESOURCE_SIZE)
        self.assertEqual(resource_headers["content-type"], "application/octet-stream")
        self.assertEqual(resource_headers["cache-control"], "no-store")
        self.assertEqual((image_status, image), (200, BROWSER_IMAGE))
        self.assertEqual(image_headers["content-type"], "image/png")
        self.assertEqual(image_headers["cache-control"], "public, max-age=3600")
        self.assertEqual(summary["request_count"], 0)
        self.assertEqual(summary["workload_request_count"], 7)
        self.assertFalse(summary["workload_records_truncated"])
        self.assertGreaterEqual(summary["workload_max_active"], 1)
        successful = [
            record for record in summary["workload_requests"] if record["status"] == 200
        ]
        self.assertEqual(len(successful), 2)
        self.assertEqual(successful[0]["sequence"], 7)
        self.assertEqual(successful[0]["pass"], "cold")
        self.assertLessEqual(
            successful[0]["monotonic_start_ns"],
            successful[0]["monotonic_end_ns"],
        )

        for marker in (
            b"__asterinasStartCompositeWorkload",
            b"__asterinasCompositeWorkloadSnapshot",
            b"interaction-layout",
            b"canvas-image",
            b"concurrent-resources",
            b"navigation-history",
            b"multi-context",
            b"for (let repetition = 0; repetition < repetitions; repetition++)",
            b"frame.contentWindow.history.back()",
            b"frame.contentWindow.history.forward()",
            b"frame.contentWindow.setTimeout",
            b"contexts.children.length !== 0",
        ):
            self.assertIn(marker, BROWSER_WORKLOAD)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for fixture JS syntax")
    def test_composite_workload_javascript_is_syntactically_valid(self) -> None:
        script = BROWSER_WORKLOAD.split(b"<script>\n", 1)[1].split(
            b"</script>", 1
        )[0]
        result = subprocess.run(
            [shutil.which("node") or "node", "--check"],
            input=script,
            capture_output=True,
            check=False,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    @unittest.skipUnless(shutil.which("node"), "Node is needed for fixture JS smoke")
    def test_performance_page_js_keeps_trusted_and_synthetic_samples_separate(self) -> None:
        script = BROWSER_PERF.split(b"<script>\n", 1)[1].split(b"</script>", 1)[0]
        harness = r"""
const vm = require('vm');
const fs = require('fs');
const handlers = Object.create(null);
const elements = Object.create(null);
for (const id of ['#timing-token', '#timing-input', '#timing-pointer']) {
  elements[id] = {
    textContent: '0',
    addEventListener(name, handler) { handlers[id + ':' + name] = handler; }
  };
}
let now = 0;
const sandbox = {
  document: {
    querySelector(id) { return elements[id]; },
    addEventListener(name, handler) { handlers['document:' + name] = handler; }
  },
  performance: {now() { return ++now; }},
  requestAnimationFrame(callback) { setTimeout(callback, 0); },
  window: {}, Promise, Number, Object, String, Error
};
vm.runInNewContext(fs.readFileSync(0, 'utf8'), sandbox);
handlers['#timing-input:input']({isTrusted: true});
handlers['#timing-pointer:pointermove']({isTrusted: true});
handlers['document:scroll']({isTrusted: true});
(async () => {
  await sandbox.window.__asterinasRunSyntheticTiming(1);
  const samples = sandbox.window.__asterinasTimingSnapshot().samples;
  if (samples.length !== 6 || samples.filter(s => s.source === 'trusted').length !== 3 ||
      samples.filter(s => s.source === 'synthetic').length !== 3 ||
      samples.some(s => s.nextRafMs < s.firstRafMs)) process.exitCode = 1;
})().catch(() => { process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node") or "node", "-e", harness],
            input=script,
            capture_output=True,
            check=False,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

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
