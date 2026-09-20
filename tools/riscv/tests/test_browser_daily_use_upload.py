#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the closed Firefox daily-use evidence upload bundle."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import http.server
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tools.riscv.tests.test_browser_daily_use_contract import (
    complete_result,
    set_group,
)


EXPERIMENT_ID = "fedcba9876543210fedcba9876543210"
GATE_RUN_ID = "0123456789abcdef0123456789abcdef"
COMPONENT_NAMES = (
    "browser-fixture-capture.json",
    "browser-local-capture.json",
    "browser-context-switch.json",
    "browser-composite-capture.json",
    "browser-system-time.json",
    "browser-thread-time.json",
)
RESULT_NAME = "browser-daily-use-result.json"
SUCCESS_NAMES = (*COMPONENT_NAMES, RESULT_NAME)


def canonical_gate_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()


class BrowserDailyUseUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.evidence = Path(self.temporary.name)

    def load_module(self):
        try:
            return importlib.import_module(
                "tools.riscv.debian.rootfs.browser_daily_use_upload"
            )
        except ModuleNotFoundError:
            self.fail("browser_daily_use_upload module is missing")

    def write_private(self, name: str, payload: bytes) -> None:
        path = self.evidence / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)

    def replace_private(self, name: str, payload: bytes) -> None:
        (self.evidence / name).unlink()
        self.write_private(name, payload)

    def make_success_evidence(self) -> dict[str, bytes]:
        payloads = {
            name: canonical_gate_json({"artifact": name, "sample": index})
            for index, name in enumerate(COMPONENT_NAMES)
        }
        result = complete_result()
        result["artifacts"] = [
            {
                "name": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in payloads.items()
        ]
        result["attribution"] = {
            "compositeArtifact": COMPONENT_NAMES[3],
            "systemArtifact": COMPONENT_NAMES[4],
            "threadArtifact": COMPONENT_NAMES[5],
        }
        payloads[RESULT_NAME] = canonical_gate_json(result)
        for name, payload in payloads.items():
            self.write_private(name, payload)
        return payloads

    def make_failure_evidence(
        self,
        *,
        completed_phases: list[str] | None = None,
        function_groups: list[dict[str, object]] | None = None,
    ) -> bytes:
        payload = canonical_gate_json(
            {
                "schemaVersion": 1,
                "runId": GATE_RUN_ID,
                "completedPhases": completed_phases or ["session"],
                "functionGroups": function_groups or [],
                "failure": {"type": "daily-use-gate", "reason": "phase-failed"},
            }
        )
        self.write_private("browser-daily-use-checkpoint.json", payload)
        return payload

    def test_build_and_parse_success_bundle_preserves_closed_order(self) -> None:
        expected = self.make_success_evidence()
        module = self.load_module()

        raw = module.build_bundle(self.evidence, EXPERIMENT_ID, "pass")
        parsed = module.parse_bundle(raw, EXPERIMENT_ID)

        self.assertEqual(parsed.experiment_id, EXPERIMENT_ID)
        self.assertEqual(parsed.gate_run_id, GATE_RUN_ID)
        self.assertEqual(parsed.outcome, "pass")
        self.assertEqual(tuple(parsed.artifacts), SUCCESS_NAMES)
        self.assertEqual(parsed.artifacts, expected)

    def test_failure_bundle_contains_only_checkpoint(self) -> None:
        expected = self.make_failure_evidence()
        module = self.load_module()

        try:
            raw = module.build_bundle(self.evidence, EXPERIMENT_ID, "fail")
        except module.EvidenceBundleError:
            self.fail("failure evidence bundles are not implemented")
        parsed = module.parse_bundle(raw, EXPERIMENT_ID)

        self.assertEqual(parsed.gate_run_id, GATE_RUN_ID)
        self.assertEqual(parsed.outcome, "fail")
        self.assertEqual(
            parsed.artifacts,
            {"browser-daily-use-checkpoint.json": expected},
        )

    def test_failure_bundle_preserves_optional_unsupported_groups(self) -> None:
        result = complete_result()
        for name in ("execution", "rendering-media"):
            set_group(
                result,
                name,
                "unsupported",
                "fixture-capability-unavailable",
            )
        groups = [result["functionGroups"][index] for index in (0, 1, 2, 3, 5)]
        expected = self.make_failure_evidence(
            completed_phases=["session", "samplers-ready", "fixture"],
            function_groups=groups,
        )
        module = self.load_module()

        raw = module.build_bundle(self.evidence, EXPERIMENT_ID, "fail")
        parsed = module.parse_bundle(raw, EXPERIMENT_ID)
        checkpoint = json.loads(
            parsed.artifacts["browser-daily-use-checkpoint.json"]
        )

        self.assertEqual(
            parsed.artifacts["browser-daily-use-checkpoint.json"], expected
        )
        self.assertEqual(checkpoint["functionGroups"], groups)

    def test_failure_checkpoint_rejects_invalid_group_states_and_reasons(self) -> None:
        result = complete_result()
        groups = [result["functionGroups"][index] for index in (0, 1, 2, 3, 5)]
        source = json.loads(
            self.make_failure_evidence(
                completed_phases=["session", "samplers-ready", "fixture"],
                function_groups=groups,
            )
        )
        module = self.load_module()
        invalid_values = []

        unsupported_required = json.loads(json.dumps(source))
        unsupported_required["functionGroups"][0].update(
            state="unsupported", reason="made-up-reason"
        )
        invalid_values.append(unsupported_required)
        optional_without_reason = json.loads(json.dumps(source))
        optional_without_reason["functionGroups"][2].update(
            state="unsupported", reason=None
        )
        invalid_values.append(optional_without_reason)
        optional_wrong_reason = json.loads(json.dumps(source))
        optional_wrong_reason["functionGroups"][2].update(
            state="unsupported", reason="browser-session-unavailable"
        )
        invalid_values.append(optional_wrong_reason)
        passing_with_reason = json.loads(json.dumps(source))
        passing_with_reason["functionGroups"][0]["reason"] = (
            "fixture-capability-failed"
        )
        invalid_values.append(passing_with_reason)
        unknown_state = json.loads(json.dumps(source))
        unknown_state["functionGroups"][0].update(
            state="partial", reason="fixture-capability-failed"
        )
        invalid_values.append(unknown_state)

        for value in invalid_values:
            with self.subTest(value=value):
                self.replace_private(
                    "browser-daily-use-checkpoint.json", canonical_gate_json(value)
                )
                with self.assertRaises(module.EvidenceBundleError):
                    module.build_bundle(self.evidence, EXPERIMENT_ID, "fail")

    def test_failure_checkpoint_rejects_unknown_phase_and_group_shape(self) -> None:
        source = json.loads(self.make_failure_evidence())
        module = self.load_module()
        invalid_values = []
        unknown_phase = dict(source)
        unknown_phase["completedPhases"] = ["not-a-phase"]
        invalid_values.append(unknown_phase)
        invalid_group = dict(source)
        invalid_group["functionGroups"] = [42]
        invalid_values.append(invalid_group)

        for value in invalid_values:
            with self.subTest(value=value):
                self.replace_private(
                    "browser-daily-use-checkpoint.json", canonical_gate_json(value)
                )
                with self.assertRaises(module.EvidenceBundleError):
                    module.build_bundle(self.evidence, EXPERIMENT_ID, "fail")

    def test_build_bundle_rejects_symlinked_evidence_directory(self) -> None:
        self.make_success_evidence()
        real = self.evidence / "real"
        real.mkdir()
        for name in SUCCESS_NAMES:
            (self.evidence / name).rename(real / name)
        link = self.evidence / "evidence-link"
        link.symlink_to(real, target_is_directory=True)
        module = self.load_module()

        with self.assertRaises(module.EvidenceBundleError):
            module.build_bundle(link, EXPERIMENT_ID, "pass")

    def test_build_bundle_rejects_invalid_identity_and_outcome(self) -> None:
        module = self.load_module()

        for experiment_id, outcome in (
            ("A" * 32, "pass"),
            ("a" * 31, "pass"),
            (EXPERIMENT_ID, "slow"),
        ):
            with self.subTest(experiment_id=experiment_id, outcome=outcome):
                with self.assertRaises(module.EvidenceBundleError):
                    module.build_bundle(self.evidence, experiment_id, outcome)

    def test_build_bundle_rejects_nonprivate_and_symlinked_artifact(self) -> None:
        self.make_success_evidence()
        module = self.load_module()
        first = self.evidence / COMPONENT_NAMES[0]
        first.chmod(0o644)
        with self.assertRaises(module.EvidenceBundleError):
            module.build_bundle(self.evidence, EXPERIMENT_ID, "pass")

        first.unlink()
        first.symlink_to(self.evidence / COMPONENT_NAMES[1])
        with self.assertRaises(module.EvidenceBundleError):
            module.build_bundle(self.evidence, EXPERIMENT_ID, "pass")

    def test_build_bundle_rejects_result_manifest_mismatch(self) -> None:
        self.make_success_evidence()
        module = self.load_module()
        result = json.loads((self.evidence / RESULT_NAME).read_bytes())
        result["artifacts"][0]["sha256"] = "0" * 64
        self.replace_private(RESULT_NAME, canonical_gate_json(result))

        with self.assertRaisesRegex(module.EvidenceBundleError, "manifest"):
            module.build_bundle(self.evidence, EXPERIMENT_ID, "pass")

    def test_build_bundle_normalizes_result_contract_failures(self) -> None:
        self.make_success_evidence()
        module = self.load_module()
        result = json.loads((self.evidence / RESULT_NAME).read_bytes())
        result["unexpected"] = None
        self.replace_private(RESULT_NAME, canonical_gate_json(result))

        with self.assertRaisesRegex(
            module.EvidenceBundleError,
            "daily-use result contract is invalid: daily-use result has unexpected fields",
        ):
            module.build_bundle(self.evidence, EXPERIMENT_ID, "pass")

    def test_parse_bundle_rejects_schema_order_encoding_size_and_digest_drift(self) -> None:
        self.make_success_evidence()
        module = self.load_module()
        raw = module.build_bundle(self.evidence, EXPERIMENT_ID, "pass")
        original = json.loads(raw)
        invalid_values = []

        extra = json.loads(raw)
        extra["unexpected"] = None
        invalid_values.append(extra)
        reordered = json.loads(raw)
        reordered["artifacts"][0], reordered["artifacts"][1] = (
            reordered["artifacts"][1],
            reordered["artifacts"][0],
        )
        invalid_values.append(reordered)
        invalid_base64 = json.loads(raw)
        invalid_base64["artifacts"][0]["base64"] = "***"
        invalid_values.append(invalid_base64)
        wrong_size = json.loads(raw)
        wrong_size["artifacts"][0]["bytes"] += 1
        invalid_values.append(wrong_size)
        wrong_digest = json.loads(raw)
        wrong_digest["artifacts"][0]["sha256"] = "0" * 64
        invalid_values.append(wrong_digest)
        wrong_run = json.loads(raw)
        wrong_run["gateRunId"] = "f" * 32
        invalid_values.append(wrong_run)

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(module.EvidenceBundleError):
                    module.parse_bundle(module._canonical_json(value), EXPERIMENT_ID)

        noncanonical = json.dumps(original, sort_keys=True).encode() + b"\n"
        self.assertNotEqual(noncanonical, raw)
        with self.assertRaises(module.EvidenceBundleError):
            module.parse_bundle(noncanonical, EXPERIMENT_ID)

    def test_upload_rejects_non_http_or_ambiguous_urls(self) -> None:
        module = self.load_module()
        for url in (
            "https://127.0.0.1:17894/evidence",
            "http://user@127.0.0.1:17894/evidence",
            "http://127.0.0.1:17894/evidence?query=1",
            "http://127.0.0.1:17894/evidence#fragment",
            "http://127.0.0.1/evidence",
        ):
            with self.subTest(url=url), self.assertRaises(
                module.EvidenceBundleError
            ):
                module.upload_bundle(b"{}\n", url, 1.0)

    def test_upload_posts_fixed_length_json_and_requires_empty_204(self) -> None:
        module = self.load_module()
        received: list[tuple[str, dict[str, str], bytes]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                received.append(
                    (
                        self.path,
                        {name.lower(): value for name, value in self.headers.items()},
                        self.rfile.read(length),
                    )
                )
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = b'{"schemaVersion":1}\n'
        try:
            upload = module.upload_bundle
        except AttributeError:
            self.fail("daily-use bundle upload is not implemented")

        upload(
            body,
            f"http://127.0.0.1:{server.server_port}/evidence/{EXPERIMENT_ID}",
            1.0,
        )

        self.assertEqual(received[0][0], f"/evidence/{EXPERIMENT_ID}")
        self.assertEqual(received[0][1]["content-type"], "application/json")
        self.assertEqual(received[0][1]["content-length"], str(len(body)))
        self.assertEqual(received[0][2], body)

    def test_main_builds_then_uploads_one_bundle(self) -> None:
        module = self.load_module()
        url = f"http://127.0.0.1:17894/evidence/{EXPERIMENT_ID}"
        try:
            main = module.main
        except AttributeError:
            self.fail("daily-use uploader CLI is not implemented")
        with (
            mock.patch.object(module, "build_bundle", return_value=b"bundle\n") as build,
            mock.patch.object(module, "upload_bundle") as upload,
            redirect_stdout(io.StringIO()),
        ):
            status = main(
                [
                    str(self.evidence),
                    EXPERIMENT_ID,
                    "pass",
                    url,
                    "--timeout",
                    "15",
                ]
            )

        self.assertEqual(status, 0)
        build.assert_called_once_with(self.evidence, EXPERIMENT_ID, "pass")
        upload.assert_called_once_with(b"bundle\n", url, 15.0)


if __name__ == "__main__":
    unittest.main()
