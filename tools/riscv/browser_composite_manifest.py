#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bind Firefox composite captures to exact host fixture request segments."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import stat

from tools.riscv.debian.rootfs.browser_workload_contract import (
    MODES,
    PHASES,
    WorkloadContractError,
    validate_run_id,
    validate_workload_snapshot,
)
from tools.riscv.megrez_network_fixture import is_successful_workload_summary


class EvidenceManifestError(ValueError):
    """The composite evidence set is incomplete or internally inconsistent."""


ARTIFACT_NAMES = {
    "capture": "browser-composite-capture.json",
    "checkpoint": "browser-composite-checkpoint.json",
    "system": "browser-system-time.json",
    "thread": "browser-thread-time.json",
}
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_RUNS = 16


def _regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise EvidenceManifestError("evidence artifact is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARTIFACT_BYTES:
            raise EvidenceManifestError(
                "evidence artifact is not a bounded regular file"
            )
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise EvidenceManifestError(
                    "evidence artifact changed while being read"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise EvidenceManifestError("evidence artifact changed while being read")
        return b"".join(chunks)
    except OSError as error:
        raise EvidenceManifestError("evidence artifact is unavailable") from error
    finally:
        os.close(descriptor)


def _load_json(path: Path) -> tuple[dict[str, object], bytes]:
    payload = _regular_file(path)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceManifestError("evidence artifact is not valid JSON") from error
    if not isinstance(value, dict):
        raise EvidenceManifestError("evidence artifact must be a JSON object")
    return value, payload


def _digest_entry(base: Path, path: Path, payload: bytes) -> dict[str, object]:
    try:
        relative = path.relative_to(base)
    except ValueError as error:
        raise EvidenceManifestError(
            "evidence artifact escapes its manifest directory"
        ) from error
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise EvidenceManifestError("evidence artifact path is not canonical")
    return {
        "path": relative.as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _clock_bounds(report: Mapping[str, object]) -> tuple[int, int]:
    intervals = report.get("intervals")
    if (
        not isinstance(intervals, list)
        or not intervals
        or len(intervals) > 512
        or report.get("samples") != len(intervals)
    ):
        raise EvidenceManifestError("time evidence intervals are invalid")
    prior_end = -1
    first_start = -1
    for item in intervals:
        if not isinstance(item, dict):
            raise EvidenceManifestError("time evidence interval is invalid")
        start = item.get("guest_monotonic_start_ns")
        end = item.get("guest_monotonic_end_ns")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or start < prior_end
        ):
            raise EvidenceManifestError("time evidence clock is invalid")
        if first_start < 0:
            first_start = start
        prior_end = end
    return first_start, prior_end


def _capture_identity(
    capture: Mapping[str, object], *, expected_mode: str, physical: bool
) -> tuple[tuple[int, int], tuple[int, int], str, int, int]:
    if (
        capture.get("schema_version") != 1
        or capture.get("mode") != expected_mode
        or capture.get("physical") is not physical
        or capture.get("clock_domain") != "browser-and-guest-monotonic-separated"
        or capture.get("system_artifact") != ARTIFACT_NAMES["system"]
        or capture.get("thread_artifact") != ARTIFACT_NAMES["thread"]
        or capture.get("checkpoint_artifact") != ARTIFACT_NAMES["checkpoint"]
    ):
        raise EvidenceManifestError("capture identity is invalid")
    firefox_pid = capture.get("firefox_pid")
    xorg_pid = capture.get("xorg_pid")
    starttimes = capture.get("process_starttime_ticks")
    if (
        type(firefox_pid) is not int
        or type(xorg_pid) is not int
        or firefox_pid <= 0
        or xorg_pid <= 0
        or firefox_pid == xorg_pid
        or not isinstance(starttimes, list)
        or len(starttimes) != 2
        or any(type(value) is not int or value <= 0 for value in starttimes)
    ):
        raise EvidenceManifestError("capture process identity is invalid")
    try:
        run_id = validate_run_id(capture.get("run_id"))
        workload = validate_workload_snapshot(
            capture.get("workload"),
            expected_mode=expected_mode,
            expected_run_id=run_id,
        )
    except WorkloadContractError as error:
        raise EvidenceManifestError("capture workload is invalid") from error
    workload_url = capture.get("workload_url")
    if not isinstance(workload_url, str) or not workload_url.endswith(f"?run={run_id}"):
        raise EvidenceManifestError("capture workload URL is invalid")
    if workload["state"] != "complete":
        raise EvidenceManifestError("capture workload is incomplete")
    observations = capture.get("phase_observations")
    start_ns = capture.get("workload_start_observed_guest_monotonic_ns")
    if (
        type(start_ns) is not int
        or start_ns < 0
        or not isinstance(observations, list)
        or len(observations) != len(PHASES)
    ):
        raise EvidenceManifestError("capture observation clock is invalid")
    observation_times: list[int] = []
    for expected_phase, observation in zip(PHASES, observations):
        if (
            not isinstance(observation, dict)
            or set(observation) != {"phase", "observed_guest_monotonic_ns"}
            or observation.get("phase") != expected_phase
            or type(observation.get("observed_guest_monotonic_ns")) is not int
        ):
            raise EvidenceManifestError("capture phase observation is invalid")
        observation_times.append(observation["observed_guest_monotonic_ns"])
    if (
        observation_times != sorted(observation_times)
        or observation_times[0] < start_ns
    ):
        raise EvidenceManifestError("capture observation clock regressed")
    return (
        (firefox_pid, xorg_pid),
        (starttimes[0], starttimes[1]),
        run_id,
        start_ns,
        observation_times[-1],
    )


def _validate_run(
    base: Path, run_dir: Path, *, expected_mode: str, physical: bool
) -> tuple[dict[str, object], tuple[tuple[int, int], tuple[int, int]]]:
    try:
        relative_run = run_dir.relative_to(base)
    except ValueError as error:
        raise EvidenceManifestError(
            "run directory escapes the manifest directory"
        ) from error
    run_label = "." if run_dir == base else relative_run.as_posix()
    if (
        run_dir != base
        and any(part in ("", ".", "..") for part in relative_run.parts)
        or not run_dir.is_dir()
        or run_dir.is_symlink()
    ):
        raise EvidenceManifestError("run directory is invalid")
    loaded: dict[str, dict[str, object]] = {}
    payloads: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for role, name in ARTIFACT_NAMES.items():
        path = run_dir / name
        loaded[role], payloads[role] = _load_json(path)
        paths[role] = path

    capture = loaded["capture"]
    pids, starttimes, run_id, workload_start, workload_end = _capture_identity(
        capture, expected_mode=expected_mode, physical=physical
    )
    checkpoint = loaded["checkpoint"]
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("clock_domain") != "guest-monotonic-observation"
        or checkpoint.get("mode") != expected_mode
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("completed_phases") != list(PHASES)
        or checkpoint.get("phase_observations") != capture.get("phase_observations")
        or checkpoint.get("workload") != capture.get("workload")
    ):
        raise EvidenceManifestError("checkpoint does not match capture")

    system = loaded["system"]
    thread = loaded["thread"]
    system_start, system_end = _clock_bounds(system)
    thread_start, thread_end = _clock_bounds(thread)
    if system.get("process_ids") != list(pids):
        raise EvidenceManifestError("system evidence process identity is invalid")
    if (
        thread.get("process_id") != pids[0]
        or thread.get("process_starttime_ticks") != starttimes[0]
        or thread.get("physical") is not physical
    ):
        raise EvidenceManifestError("thread evidence process identity is invalid")
    system_intervals = system["intervals"]
    if not isinstance(system_intervals, list):
        raise EvidenceManifestError("system evidence intervals are invalid")
    for interval in system_intervals:
        if not isinstance(interval, dict):
            raise EvidenceManifestError("system evidence interval is invalid")
        processes = interval.get("processes")
        if not isinstance(processes, list):
            raise EvidenceManifestError("system evidence processes are invalid")
        actual = [
            (item.get("pid"), item.get("starttime_ticks"))
            for item in processes
            if isinstance(item, dict)
        ]
        if actual != list(zip(pids, starttimes)):
            raise EvidenceManifestError("system evidence process identity changed")
    if (
        system_start > workload_start
        or system_end < workload_end
        or thread_start > workload_start
        or thread_end < workload_end
    ):
        raise EvidenceManifestError("time evidence does not cover the workload")

    artifacts = {
        role: _digest_entry(base, paths[role], payloads[role])
        for role in ARTIFACT_NAMES
    }
    return (
        {
            "run_directory": run_label,
            "run_id": run_id,
            "artifacts": artifacts,
            "guest_monotonic_ns": {
                "workload": [workload_start, workload_end],
                "system": [system_start, system_end],
                "thread": [thread_start, thread_end],
            },
        },
        (pids, starttimes),
    )


def _run_record_identity(record: Mapping[str, object]) -> tuple[str, int, str]:
    return str(record["phase"]), int(record["sequence"]), str(record["pass"])


def _expected_run_records(mode: str) -> Counter[tuple[str, int, str]]:
    shape = MODES[mode]
    expected: Counter[tuple[str, int, str]] = Counter()
    expected[("resource", 0, "cold")] += 1
    expected.update(
        ("image", sequence, "cold") for sequence in range(shape["scale"] * 4)
    )
    expected.update(
        ("resource", sequence, "cold") for sequence in range(shape["resources"])
    )
    expected.update(
        ("resource", sequence, "warm") for sequence in range(shape["resources"])
    )
    expected.update(
        ("context", sequence, "cold") for sequence in range(shape["contexts"])
    )
    return expected


def _bind_fixture_segments(
    fixture: Mapping[str, object], *, mode: str, runs: int
) -> list[dict[str, object]]:
    if not is_successful_workload_summary(fixture, expected_mode=mode, runs=runs):
        raise EvidenceManifestError(
            "fixture summary is not an exact completed workload"
        )
    records = fixture.get("workload_requests")
    if not isinstance(records, list):
        raise EvidenceManifestError("fixture records are unavailable")
    segments: list[dict[str, object]] = []
    observed_run_ids: set[str] = set()
    cursor = 0
    for index in range(runs):
        expected = _expected_run_records(mode)
        end = cursor + sum(expected.values())
        segment = records[cursor:end]
        if Counter(_run_record_identity(record) for record in segment) != expected:
            raise EvidenceManifestError("fixture run boundary is invalid")
        run_ids = {
            record.get("run_id") for record in segment if isinstance(record, dict)
        }
        if (
            len(run_ids) != 1
            or not isinstance(next(iter(run_ids)), str)
            or next(iter(run_ids)) in observed_run_ids
        ):
            raise EvidenceManifestError("fixture run identity is invalid")
        run_id = next(iter(run_ids))
        observed_run_ids.add(run_id)
        starts = [record["monotonic_start_ns"] for record in segment]
        ends = [record["monotonic_end_ns"] for record in segment]
        segment_start = min(starts)
        segment_end = max(ends)
        if (
            any(type(value) is not int for value in starts + ends)
            or index > 0
            and segment_start <= segments[-1]["host_monotonic_ns"][1]
        ):
            raise EvidenceManifestError("fixture run boundary clock is invalid")
        canonical = json.dumps(segment, sort_keys=True, separators=(",", ":")).encode()
        segments.append(
            {
                "index_range": [cursor, end],
                "run_id": run_id,
                "host_monotonic_ns": [segment_start, segment_end],
                "records_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        )
        cursor = end
    if cursor != len(records):
        raise EvidenceManifestError("fixture records do not end at a run boundary")
    return segments


def _build_manifest(
    base: Path,
    fixture_path: Path,
    run_dirs: Sequence[Path],
    *,
    expected_mode: str,
    physical: bool,
) -> dict[str, object]:
    if (
        expected_mode not in MODES
        or type(physical) is not bool
        or not 1 <= len(run_dirs) <= MAX_RUNS
        or not base.is_absolute()
        or not base.is_dir()
        or base.is_symlink()
    ):
        raise EvidenceManifestError("manifest inputs are invalid")
    resolved_dirs = [path.resolve() for path in run_dirs]
    if len(set(resolved_dirs)) != len(resolved_dirs):
        raise EvidenceManifestError("run directory is duplicated")
    fixture, fixture_payload = _load_json(fixture_path)
    fixture_segments = _bind_fixture_segments(
        fixture, mode=expected_mode, runs=len(resolved_dirs)
    )
    bound_runs: list[dict[str, object]] = []
    stable_identity: tuple[tuple[int, int], tuple[int, int]] | None = None
    for index, run_dir in enumerate(resolved_dirs):
        bound, identity = _validate_run(
            base, run_dir, expected_mode=expected_mode, physical=physical
        )
        if stable_identity is None:
            stable_identity = identity
        elif identity != stable_identity:
            raise EvidenceManifestError("Firefox/Xorg identity changed between runs")
        bound["index"] = index + 1
        if bound["run_id"] != fixture_segments[index]["run_id"]:
            raise EvidenceManifestError("capture and fixture run identities differ")
        bound["fixture_records"] = fixture_segments[index]
        bound_runs.append(bound)
    if stable_identity is None:
        raise EvidenceManifestError("manifest has no runs")
    return {
        "schema_version": 1,
        "mode": expected_mode,
        "physical": physical,
        "process_identity": {
            "pids": list(stable_identity[0]),
            "starttime_ticks": list(stable_identity[1]),
        },
        "fixture": _digest_entry(base, fixture_path, fixture_payload),
        "runs": bound_runs,
    }


def _write_private_json(path: Path, value: Mapping[str, object]) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise EvidenceManifestError("manifest output is not exclusive") from error
    try:
        cursor = 0
        while cursor < len(payload):
            written = os.write(descriptor, payload[cursor:])
            if written <= 0:
                raise EvidenceManifestError("manifest output did not advance")
            cursor += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def create_manifest(
    artifact_dir: Path,
    fixture_path: Path,
    run_dirs: Sequence[Path],
    *,
    expected_mode: str,
    physical: bool,
    output_path: Path | None = None,
) -> dict[str, object]:
    """Validate an evidence set and optionally publish its deterministic manifest."""

    base = artifact_dir.resolve()
    fixture = fixture_path.resolve()
    manifest = _build_manifest(
        base,
        fixture,
        run_dirs,
        expected_mode=expected_mode,
        physical=physical,
    )
    if output_path is not None:
        output = output_path.resolve()
        try:
            output.relative_to(base)
        except ValueError as error:
            raise EvidenceManifestError(
                "manifest output escapes its directory"
            ) from error
        if os.path.lexists(output):
            raise EvidenceManifestError("manifest output already exists")
        _write_private_json(output, manifest)
    return manifest


def _manifest_relative_path(base: Path, value: object) -> Path:
    if not isinstance(value, str):
        raise EvidenceManifestError("manifest artifact path is invalid")
    if value == ".":
        return base
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise EvidenceManifestError("manifest artifact path is invalid")
    candidate = base / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, ValueError) as error:
        raise EvidenceManifestError(
            "manifest artifact escapes its directory"
        ) from error
    return resolved


def verify_manifest(manifest_path: Path) -> dict[str, object]:
    """Rebuild a manifest from its named artifacts and require byte equality."""

    manifest, _ = _load_json(manifest_path.resolve())
    base = manifest_path.resolve().parent
    if set(manifest) != {
        "schema_version",
        "mode",
        "physical",
        "process_identity",
        "fixture",
        "runs",
    }:
        raise EvidenceManifestError("manifest fields are invalid")
    fixture_entry = manifest.get("fixture")
    runs = manifest.get("runs")
    if not isinstance(fixture_entry, dict) or not isinstance(runs, list):
        raise EvidenceManifestError("manifest contents are invalid")
    fixture_path = _manifest_relative_path(base, fixture_entry.get("path"))
    run_dirs: list[Path] = []
    for run in runs:
        if not isinstance(run, dict):
            raise EvidenceManifestError("manifest run is invalid")
        run_dirs.append(_manifest_relative_path(base, run.get("run_directory")))
    rebuilt = _build_manifest(
        base,
        fixture_path,
        run_dirs,
        expected_mode=str(manifest.get("mode")),
        physical=manifest.get("physical"),
    )
    if rebuilt != manifest:
        raise EvidenceManifestError("manifest digest or evidence metadata changed")
    return rebuilt


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--fixture-summary", type=Path)
    parser.add_argument("--run-dir", type=Path, action="append", default=[])
    parser.add_argument("--mode", choices=tuple(MODES))
    parser.add_argument("--physical", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    options = parser.parse_args(arguments)
    try:
        if options.verify is not None:
            if (
                any(
                    value is not None
                    for value in (
                        options.artifact_dir,
                        options.fixture_summary,
                        options.mode,
                        options.output,
                    )
                )
                or options.run_dir
                or options.physical
            ):
                parser.error("--verify cannot be combined with creation arguments")
            manifest = verify_manifest(options.verify)
        else:
            if (
                any(
                    value is None
                    for value in (
                        options.artifact_dir,
                        options.fixture_summary,
                        options.mode,
                        options.output,
                    )
                )
                or not options.run_dir
            ):
                parser.error(
                    "creation requires artifact, fixture, run, mode, and output"
                )
            manifest = create_manifest(
                options.artifact_dir,
                options.fixture_summary,
                options.run_dir,
                expected_mode=options.mode,
                physical=options.physical,
                output_path=options.output,
            )
    except (EvidenceManifestError, OSError) as error:
        parser.exit(1, f"browser composite manifest: {error}\n")
    print(
        "ASTERINAS_BROWSER_COMPOSITE_MANIFEST_PASS "
        f"mode={manifest['mode']} runs={len(manifest['runs'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
