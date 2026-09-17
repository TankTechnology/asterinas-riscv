#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate bounded snapshots from the local Firefox composite workload."""

from __future__ import annotations

import math
import re


class WorkloadContractError(ValueError):
    """The browser workload snapshot is malformed or outside its bounds."""


MODES = {
    "smoke": {
        "scale": 1,
        "nodes": 128,
        "resources": 8,
        "contexts": 2,
        "deadline_seconds": 30,
    },
    "profile": {
        "scale": 4,
        "nodes": 512,
        "resources": 32,
        "contexts": 3,
        "deadline_seconds": 120,
    },
    "stress": {
        "scale": 12,
        "nodes": 1024,
        "resources": 96,
        "contexts": 3,
        "deadline_seconds": 300,
    },
}
PHASES = (
    "warmup",
    "interaction-layout",
    "canvas-image",
    "concurrent-resources",
    "navigation-history",
    "multi-context",
    "cooldown",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schemaVersion",
        "workloadVersion",
        "clockDomain",
        "runId",
        "mode",
        "state",
        "phases",
        "error",
    }
)
_PHASE_FIELDS = frozenset({"name", "state", "startMs", "endMs", "metrics"})
_METRIC_FIELDS = frozenset(
    {
        "operationCount",
        "requestCount",
        "contextCount",
        "longFrameCount",
        "frameMs",
    }
)
_ERROR_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def validate_run_id(value: object) -> str:
    """Return one canonical unpredictable workload-run identity."""

    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise WorkloadContractError("workload run identity is invalid")
    return value


def _finite_time(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 3_600_000
    ):
        raise WorkloadContractError(f"{label} is outside the workload clock")
    return float(value)


def _bounded_count(value: object, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise WorkloadContractError(f"{label} is outside its bound")
    return value


def expected_phase_metrics(mode: str, phase: str) -> dict[str, int]:
    """Return the exact successful work encoded by the fixture program."""

    if mode not in MODES or phase not in PHASES:
        raise WorkloadContractError("workload expectation identity is invalid")
    config = MODES[mode]
    scale = config["scale"]
    nodes = config["nodes"]
    resources = config["resources"]
    contexts = config["contexts"]
    expectations = {
        "warmup": (nodes, 1, 0, 1),
        "interaction-layout": (
            scale * 8 * (nodes // 2 + 2),
            0,
            0,
            scale * 8,
        ),
        "canvas-image": (scale * 200, scale * 4, 0, 1),
        "concurrent-resources": (resources * 3, resources * 3, 0, 0),
        "navigation-history": (4 + scale * 4, 2, 1, 0),
        "multi-context": (contexts * 2, contexts, contexts, 1),
        "cooldown": (2, 0, 0, 1),
    }
    operations, requests, context_count, frame_samples = expectations[phase]
    return {
        "operationCount": operations,
        "requestCount": requests,
        "contextCount": context_count,
        "frameSamples": frame_samples,
    }


def _normalize_metrics(
    value: object, *, expected: dict[str, int], complete: bool
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _METRIC_FIELDS:
        raise WorkloadContractError("phase metrics have unexpected fields")
    frames = value["frameMs"]
    if not isinstance(frames, list) or len(frames) > 256:
        raise WorkloadContractError("frame samples exceed their bound")
    normalized_frames = [_finite_time(sample, "frame sample") for sample in frames]
    if any(sample > 60_000 for sample in normalized_frames):
        raise WorkloadContractError("frame sample exceeds its latency bound")
    normalized: dict[str, object] = {
        "operationCount": _bounded_count(
            value["operationCount"],
            "operation count",
            1_000_000,
        ),
        "requestCount": _bounded_count(
            value["requestCount"],
            "request count",
            384,
        ),
        "contextCount": _bounded_count(
            value["contextCount"],
            "context count",
            3,
        ),
        "longFrameCount": _bounded_count(
            value["longFrameCount"],
            "long frame count",
            expected["frameSamples"],
        ),
        "frameMs": normalized_frames,
    }
    if normalized["longFrameCount"] != sum(sample > 50 for sample in normalized_frames):
        raise WorkloadContractError("long frame count disagrees with samples")
    if len(normalized_frames) > expected["frameSamples"]:
        raise WorkloadContractError("phase frame sample count exceeds exact workload")
    if any(
        normalized[name] > expected[name]
        for name in ("operationCount", "requestCount", "contextCount")
    ):
        raise WorkloadContractError("phase metrics exceed exact workload")
    if complete:
        for name in ("operationCount", "requestCount", "contextCount"):
            if normalized[name] != expected[name]:
                raise WorkloadContractError("phase metrics do not match exact workload")
        if len(normalized_frames) != expected["frameSamples"]:
            raise WorkloadContractError("phase frame sample count is not exact")
    return normalized


def validate_workload_snapshot(
    value: object,
    *,
    expected_mode: str,
    expected_run_id: str | None = None,
    allow_running: bool = False,
) -> dict[str, object]:
    """Return one detached snapshot after enforcing the complete protocol."""

    if expected_mode not in MODES:
        raise WorkloadContractError("expected workload mode is invalid")
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL_FIELDS:
        raise WorkloadContractError("workload snapshot has unexpected fields")
    if (
        value["schemaVersion"] != 1
        or value["workloadVersion"] != 1
        or value["clockDomain"] != "browser-performance-now"
        or value["mode"] != expected_mode
    ):
        raise WorkloadContractError("workload snapshot identity is invalid")
    run_id = validate_run_id(value["runId"])
    if expected_run_id is not None and run_id != validate_run_id(expected_run_id):
        raise WorkloadContractError("workload run identity changed")
    state = value["state"]
    if state not in {"running", "complete", "failed"}:
        raise WorkloadContractError("workload state is invalid")
    if state == "running" and not allow_running:
        raise WorkloadContractError("workload is not complete")

    error = value["error"]
    if state == "failed":
        if (
            not isinstance(error, str)
            or len(error) > 96
            or _ERROR_TOKEN.fullmatch(error) is None
        ):
            raise WorkloadContractError("workload failure token is invalid")
    elif error is not None:
        raise WorkloadContractError("nonfailed workload has an error")

    phases = value["phases"]
    if not isinstance(phases, list) or not phases or len(phases) > len(PHASES):
        raise WorkloadContractError("workload phase list is invalid")
    names = tuple(
        item.get("name") if isinstance(item, dict) else None for item in phases
    )
    if names != PHASES[: len(phases)]:
        raise WorkloadContractError("workload phases are missing or reordered")
    if state == "complete" and len(phases) != len(PHASES):
        raise WorkloadContractError("complete workload lacks terminal phases")

    normalized_phases: list[dict[str, object]] = []
    prior_end = 0.0
    for index, item in enumerate(phases):
        if not isinstance(item, dict) or set(item) != _PHASE_FIELDS:
            raise WorkloadContractError("workload phase has unexpected fields")
        phase_state = item["state"]
        is_last = index == len(phases) - 1
        allowed_states = {"complete"}
        if is_last and state == "running":
            allowed_states.add("running")
        if is_last and state == "failed":
            allowed_states.add("failed")
        if phase_state not in allowed_states:
            raise WorkloadContractError("phase state disagrees with workload state")

        start = _finite_time(item["startMs"], "phase start")
        if start < prior_end:
            raise WorkloadContractError("phase clock moved backwards")
        if phase_state == "running":
            if item["endMs"] is not None:
                raise WorkloadContractError("running phase has an end time")
            end: float | None = None
            prior_end = start
        else:
            end = _finite_time(item["endMs"], "phase end")
            if end < start:
                raise WorkloadContractError("phase end precedes its start")
            prior_end = end
        normalized_phases.append(
            {
                "name": item["name"],
                "state": phase_state,
                "startMs": start,
                "endMs": end,
                "metrics": _normalize_metrics(
                    item["metrics"],
                    expected=expected_phase_metrics(expected_mode, str(item["name"])),
                    complete=phase_state == "complete",
                ),
            }
        )

    return {
        "schemaVersion": 1,
        "workloadVersion": 1,
        "clockDomain": "browser-performance-now",
        "runId": run_id,
        "mode": expected_mode,
        "state": state,
        "phases": normalized_phases,
        "error": error,
    }
