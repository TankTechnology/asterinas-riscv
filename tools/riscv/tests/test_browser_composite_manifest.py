#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for binding Firefox composite captures to fixture request records."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.riscv.browser_composite_manifest import (
    EvidenceManifestError,
    create_manifest,
    verify_manifest,
)
from tools.riscv.debian.rootfs.browser_workload_contract import (
    PHASES,
    expected_phase_metrics,
)
from tools.riscv.megrez_network_fixture import (
    BROWSER_IMAGE,
    WORKLOAD_RESOURCE_SIZE,
)


def _run_id(index: int) -> str:
    return f"{index:032x}"


def _workload(mode: str = "smoke", run_id: str = _run_id(1)) -> dict[str, object]:
    phases: list[dict[str, object]] = []
    for index, name in enumerate(PHASES):
        expected = expected_phase_metrics(mode, name)
        frames = [4.0] * expected["frameSamples"]
        phases.append(
            {
                "name": name,
                "state": "complete",
                "startMs": float(index * 10),
                "endMs": float(index * 10 + 5),
                "metrics": {
                    "operationCount": expected["operationCount"],
                    "requestCount": expected["requestCount"],
                    "contextCount": expected["contextCount"],
                    "longFrameCount": 0,
                    "frameMs": frames,
                },
            }
        )
    return {
        "schemaVersion": 1,
        "workloadVersion": 1,
        "clockDomain": "browser-performance-now",
        "runId": run_id,
        "mode": mode,
        "state": "complete",
        "phases": phases,
        "error": None,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _write_run(root: Path, index: int, identity: tuple[int, int] = (11, 22)) -> Path:
    run = root / f"run-{index}"
    run.mkdir()
    run_id = _run_id(index)
    workload = _workload(run_id=run_id)
    observations = [
        {"phase": name, "observed_guest_monotonic_ns": 200 + offset * 10}
        for offset, name in enumerate(PHASES)
    ]
    capture = {
        "schema_version": 1,
        "clock_domain": "browser-and-guest-monotonic-separated",
        "workload_url": (
            "http://10.0.2.2:17894/browser-quality/workload.html?run=" + run_id
        ),
        "run_id": run_id,
        "workload_start_observed_guest_monotonic_ns": 150,
        "workload": workload,
        "phase_observations": observations,
        "firefox_pid": 101,
        "xorg_pid": 202,
        "process_starttime_ticks": list(identity),
        "mode": "smoke",
        "physical": True,
        "system_artifact": "browser-system-time.json",
        "thread_artifact": "browser-thread-time.json",
        "checkpoint_artifact": "browser-composite-checkpoint.json",
    }
    checkpoint = {
        "schema_version": 1,
        "clock_domain": "guest-monotonic-observation",
        "mode": "smoke",
        "run_id": run_id,
        "completed_phases": list(PHASES),
        "phase_observations": observations,
        "workload": workload,
    }
    interval = {
        "guest_monotonic_start_ns": 100,
        "guest_monotonic_end_ns": 300,
        "processes": [
            {"pid": 101, "starttime_ticks": identity[0]},
            {"pid": 202, "starttime_ticks": identity[1]},
        ],
    }
    system = {
        "schema_version": 1,
        "samples": 1,
        "process_ids": [101, 202],
        "intervals": [interval],
    }
    thread = {
        "schema_version": 1,
        "samples": 1,
        "physical": True,
        "process_id": 101,
        "process_starttime_ticks": identity[0],
        "intervals": [
            {
                "guest_monotonic_start_ns": 100,
                "guest_monotonic_end_ns": 300,
            }
        ],
    }
    _write_json(run / "browser-composite-capture.json", capture)
    _write_json(run / "browser-composite-checkpoint.json", checkpoint)
    _write_json(run / "browser-system-time.json", system)
    _write_json(run / "browser-thread-time.json", thread)
    return run


def _record(
    phase: str, sequence: int, pass_name: str, monotonic_ns: int, run_id: str
) -> dict[str, object]:
    return {
        "active_at_start": 1,
        "body_bytes": len(BROWSER_IMAGE)
        if phase == "image"
        else WORKLOAD_RESOURCE_SIZE,
        "mode": "smoke",
        "monotonic_end_ns": monotonic_ns + 1,
        "monotonic_start_ns": monotonic_ns,
        "pass": pass_name,
        "phase": phase,
        "run_id": run_id,
        "sequence": sequence,
        "status": 200,
    }


def _run_records(index: int) -> list[dict[str, object]]:
    base = index * 10_000
    identities: list[tuple[str, int, str]] = [("resource", 0, "cold")]
    identities.extend(("image", sequence, "cold") for sequence in range(4))
    identities.extend(("resource", sequence, "cold") for sequence in range(8))
    identities.extend(("resource", sequence, "warm") for sequence in range(8))
    identities.extend(("context", sequence, "cold") for sequence in range(2))
    return [
        _record(phase, sequence, pass_name, base + offset * 10, _run_id(index + 1))
        for offset, (phase, sequence, pass_name) in enumerate(identities)
    ]


def _fixture(runs: int) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for index in range(runs):
        records.extend(_run_records(index))
    return {
        "schema_version": 1,
        "workload_request_count": len(records),
        "workload_records_truncated": False,
        "workload_max_active": 1,
        "workload_requests": records,
    }


class BrowserCompositeManifestTests(unittest.TestCase):
    def test_create_and_verify_bind_every_artifact_and_per_run_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dirs = [_write_run(root, 1), _write_run(root, 2)]
            fixture = root / "physical-fixture.json"
            _write_json(fixture, _fixture(2))

            manifest_path = root / "manifest.json"
            manifest = create_manifest(
                root,
                fixture,
                run_dirs,
                expected_mode="smoke",
                physical=True,
                output_path=manifest_path,
            )

            self.assertEqual(
                [item["fixture_records"]["index_range"] for item in manifest["runs"]],
                [[0, 23], [23, 46]],
            )
            self.assertEqual(len(manifest["runs"][0]["artifacts"]), 4)
            self.assertEqual(verify_manifest(manifest_path), manifest)

    def test_verify_rejects_any_artifact_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = _write_run(root, 1)
            fixture = root / "physical-fixture.json"
            _write_json(fixture, _fixture(1))
            manifest_path = root / "manifest.json"
            create_manifest(
                root,
                fixture,
                [run],
                expected_mode="smoke",
                physical=True,
                output_path=manifest_path,
            )
            with (run / "browser-system-time.json").open("a") as stream:
                stream.write(" ")

            with self.assertRaisesRegex(EvidenceManifestError, "digest"):
                verify_manifest(manifest_path)

    def test_create_rejects_aggregate_valid_but_cross_run_fixture_mix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dirs = [_write_run(root, 1), _write_run(root, 2)]
            summary = _fixture(2)
            records = summary["workload_requests"]
            assert isinstance(records, list)
            records[20], records[30] = records[30], records[20]
            fixture = root / "physical-fixture.json"
            _write_json(fixture, summary)

            with self.assertRaisesRegex(EvidenceManifestError, "run boundary"):
                create_manifest(
                    root,
                    fixture,
                    run_dirs,
                    expected_mode="smoke",
                    physical=True,
                )

    def test_create_rejects_process_identity_drift_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dirs = [_write_run(root, 1), _write_run(root, 2, (33, 44))]
            fixture = root / "physical-fixture.json"
            _write_json(fixture, _fixture(2))

            with self.assertRaisesRegex(EvidenceManifestError, "identity changed"):
                create_manifest(
                    root,
                    fixture,
                    run_dirs,
                    expected_mode="smoke",
                    physical=True,
                )

    def test_create_rejects_reordered_guest_runs_against_fixture_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dirs = [_write_run(root, 1), _write_run(root, 2)]
            fixture = root / "physical-fixture.json"
            _write_json(fixture, _fixture(2))

            with self.assertRaisesRegex(EvidenceManifestError, "identities differ"):
                create_manifest(
                    root,
                    fixture,
                    list(reversed(run_dirs)),
                    expected_mode="smoke",
                    physical=True,
                )

    def test_verify_rejects_fixture_through_escaping_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "evidence"
            root.mkdir()
            run = _write_run(root, 1)
            nested = root / "nested"
            nested.mkdir()
            fixture = nested / "physical-fixture.json"
            _write_json(fixture, _fixture(1))
            manifest_path = root / "manifest.json"
            create_manifest(
                root,
                fixture,
                [run],
                expected_mode="smoke",
                physical=True,
                output_path=manifest_path,
            )
            outside = parent / "outside"
            nested.rename(outside)
            nested.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(EvidenceManifestError, "escapes"):
                verify_manifest(manifest_path)

    def test_verify_rejects_artifact_replaced_by_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = _write_run(root, 1)
            fixture = root / "physical-fixture.json"
            _write_json(fixture, _fixture(1))
            manifest_path = root / "manifest.json"
            create_manifest(
                root,
                fixture,
                [run],
                expected_mode="smoke",
                physical=True,
                output_path=manifest_path,
            )
            artifact = run / "browser-system-time.json"
            replacement = root / "replacement.json"
            artifact.rename(replacement)
            artifact.symlink_to(replacement)

            with self.assertRaisesRegex(EvidenceManifestError, "unavailable"):
                verify_manifest(manifest_path)

    def test_create_accepts_concurrent_records_in_completion_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = _write_run(root, 1)
            summary = _fixture(1)
            records = summary["workload_requests"]
            assert isinstance(records, list)
            records[0], records[1] = records[1], records[0]
            fixture = root / "physical-fixture.json"
            _write_json(fixture, summary)

            manifest = create_manifest(
                root,
                fixture,
                [run],
                expected_mode="smoke",
                physical=True,
            )

            self.assertEqual(
                manifest["runs"][0]["fixture_records"]["host_monotonic_ns"],
                [0, 221],
            )


if __name__ == "__main__":
    unittest.main()
