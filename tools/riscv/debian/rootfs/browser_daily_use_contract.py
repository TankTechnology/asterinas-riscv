#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate bounded evidence summaries for one Firefox daily-use run."""

from __future__ import annotations

import math
import re


FUNCTION_GROUPS = (
    "document",
    "storage",
    "execution",
    "rendering-media",
    "navigation",
    "download",
    "contexts",
)
PERFORMANCE_CATEGORIES = (
    "startup",
    "input",
    "scroll",
    "navigation",
    "context-switch",
)

MAX_DURATION_MS = 3_600_000.0
MAX_ARTIFACTS = 16
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_LIMITATIONS = 16
MAX_REASON_LENGTH = 96
MAX_ARTIFACT_NAME_LENGTH = 128

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schemaVersion",
        "runId",
        "state",
        "identities",
        "functionGroups",
        "performance",
        "slowCount",
        "artifacts",
        "attribution",
        "limitations",
    }
)
_IDENTITIES_FIELDS = frozenset({"firefox", "xorg"})
_PROCESS_IDENTITY_FIELDS = frozenset({"initial", "final"})
_IDENTITY_SNAPSHOT_FIELDS = frozenset({"pid", "startTimeTicks"})
_FUNCTION_GROUP_FIELDS = frozenset({"name", "state", "reason"})
_PERFORMANCE_FIELDS = frozenset(
    {"name", "state", "clockDomain", "metrics", "reason"}
)
_ARTIFACT_FIELDS = frozenset({"name", "bytes", "sha256"})
_ATTRIBUTION_FIELDS = frozenset(
    {"compositeArtifact", "systemArtifact", "threadArtifact"}
)
_LIMITATIONS_FIELDS = frozenset({"items"})

_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_ARTIFACT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_PERFORMANCE_RULES = {
    "startup": (
        "guest-monotonic",
        frozenset({"durationMs"}),
        "durationMs",
        None,
    ),
    "input": (
        "browser-performance-now",
        frozenset({"p95Ms"}),
        "p95Ms",
        100.0,
    ),
    "scroll": (
        "browser-performance-now",
        frozenset({"p95Ms"}),
        "p95Ms",
        100.0,
    ),
    "navigation": (
        "browser-navigation",
        frozenset({"domReadyMs"}),
        "domReadyMs",
        2_000.0,
    ),
    "context-switch": (
        "guest-monotonic",
        frozenset({"durationMs"}),
        "durationMs",
        500.0,
    ),
}
_FUNCTION_GROUP_REASONS = frozenset(
    {
        "browser-session-unavailable",
        "context-cleanup-failed",
        "download-verification-failed",
        "fixture-capability-failed",
        "fixture-capability-unavailable",
        "fixture-navigation-failed",
    }
)
_PERFORMANCE_REASONS = frozenset(
    {
        "browser-clock-unavailable",
        "guest-clock-unavailable",
        "measurement-unavailable",
        "navigation-timing-invalid",
        "physical-scanout-unsupported",
        "sampler-unavailable",
    }
)
_LIMITATION_REASONS = frozenset(
    {
        "guest-and-browser-clocks-separated",
        "kernel-diagnostics-unavailable",
        "physical-scanout-unsupported",
        "public-network-excluded",
        "synthetic-input-timing",
    }
)


class DailyUseContractError(ValueError):
    """The daily-use result is malformed or does not preserve its evidence."""


def _is_exact_dict(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise DailyUseContractError(f"{label} has unexpected fields")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= (2**63 - 1):
        raise DailyUseContractError(f"{label} is invalid")
    return value


def _duration_ms(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DailyUseContractError(f"{label} is not numeric")
    try:
        result = float(value)
    except OverflowError as error:
        raise DailyUseContractError(f"{label} is outside its bound") from error
    if not math.isfinite(result) or not 0 <= result <= MAX_DURATION_MS:
        raise DailyUseContractError(f"{label} is outside its bound")
    return result


def _reason(value: object, label: str, allowed: frozenset[str]) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_REASON_LENGTH
        or _REASON.fullmatch(value) is None
    ):
        raise DailyUseContractError(f"{label} is invalid")
    if value not in allowed:
        raise DailyUseContractError(f"{label} is unsupported")
    return value


def _normalize_identity_snapshot(value: object, label: str) -> dict[str, int]:
    item = _is_exact_dict(value, _IDENTITY_SNAPSHOT_FIELDS, label)
    return {
        "pid": _positive_int(item["pid"], f"{label} pid"),
        "startTimeTicks": _positive_int(
            item["startTimeTicks"], f"{label} start time"
        ),
    }


def _normalize_process_identity(value: object, label: str) -> dict[str, dict[str, int]]:
    item = _is_exact_dict(value, _PROCESS_IDENTITY_FIELDS, label)
    initial = _normalize_identity_snapshot(item["initial"], f"{label} initial")
    final = _normalize_identity_snapshot(item["final"], f"{label} final")
    if final != initial:
        raise DailyUseContractError(f"{label} changed during the daily-use run")
    return {"initial": initial, "final": final}


def _normalize_function_groups(value: object) -> list[dict[str, str | None]]:
    if type(value) is not list or len(value) != len(FUNCTION_GROUPS):
        raise DailyUseContractError("function groups are missing or reordered")
    normalized: list[dict[str, str | None]] = []
    for expected_name, entry in zip(FUNCTION_GROUPS, value):
        item = _is_exact_dict(entry, _FUNCTION_GROUP_FIELDS, "function group")
        if item["name"] != expected_name:
            raise DailyUseContractError("function groups are missing or reordered")
        state = item["state"]
        if state not in {"pass", "fail", "unsupported"}:
            raise DailyUseContractError("function group state is invalid")
        reason = item["reason"]
        if state == "pass":
            if reason is not None:
                raise DailyUseContractError("passing function group has a reason")
            normalized_reason = None
        else:
            normalized_reason = _reason(
                reason,
                "function group reason",
                _FUNCTION_GROUP_REASONS,
            )
        normalized.append(
            {"name": expected_name, "state": state, "reason": normalized_reason}
        )
    return normalized


def _normalize_performance(value: object) -> list[dict[str, object]]:
    if type(value) is not list or len(value) != len(PERFORMANCE_CATEGORIES):
        raise DailyUseContractError("performance categories are missing or reordered")
    normalized: list[dict[str, object]] = []
    for expected_name, entry in zip(PERFORMANCE_CATEGORIES, value):
        item = _is_exact_dict(entry, _PERFORMANCE_FIELDS, "performance category")
        if item["name"] != expected_name:
            raise DailyUseContractError("performance categories are missing or reordered")
        clock_domain, metric_fields, metric_name, threshold = _PERFORMANCE_RULES[
            expected_name
        ]
        if item["clockDomain"] != clock_domain:
            raise DailyUseContractError("performance category clock domain is invalid")
        state = item["state"]
        if state not in {"pass", "slow", "unsupported"}:
            raise DailyUseContractError("performance category state is invalid")
        metrics = item["metrics"]
        reason = item["reason"]
        if state == "unsupported":
            if type(metrics) is not dict or metrics:
                raise DailyUseContractError("unsupported performance has metrics")
            normalized_metrics: dict[str, float] = {}
            normalized_reason: str | None = _reason(
                reason,
                "performance reason",
                _PERFORMANCE_REASONS,
            )
        else:
            metric_values = _is_exact_dict(metrics, metric_fields, "performance metrics")
            metric_value = _duration_ms(metric_values[metric_name], metric_name)
            if reason is not None:
                raise DailyUseContractError("measured performance has a reason")
            if threshold is None:
                if state != "pass":
                    raise DailyUseContractError("startup does not have a slow threshold")
            elif (metric_value > threshold) != (state == "slow"):
                raise DailyUseContractError("performance state disagrees with threshold")
            normalized_metrics = {metric_name: metric_value}
            normalized_reason = None
        normalized.append(
            {
                "name": expected_name,
                "state": state,
                "clockDomain": clock_domain,
                "metrics": normalized_metrics,
                "reason": normalized_reason,
            }
        )
    return normalized


def _normalize_artifacts(value: object) -> list[dict[str, object]]:
    if type(value) is not list or not 3 <= len(value) <= MAX_ARTIFACTS:
        raise DailyUseContractError("artifact list is outside its bound")
    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for entry in value:
        item = _is_exact_dict(entry, _ARTIFACT_FIELDS, "artifact")
        name = item["name"]
        if (
            not isinstance(name, str)
            or len(name) > MAX_ARTIFACT_NAME_LENGTH
            or ".." in name
            or _ARTIFACT_NAME.fullmatch(name) is None
        ):
            raise DailyUseContractError("artifact name is not canonical")
        if name in names:
            raise DailyUseContractError("artifact names are not unique")
        byte_count = item["bytes"]
        if type(byte_count) is not int or not 0 <= byte_count <= MAX_ARTIFACT_BYTES:
            raise DailyUseContractError("artifact size is outside its bound")
        digest = item["sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise DailyUseContractError("artifact SHA-256 is invalid")
        names.add(name)
        normalized.append({"name": name, "bytes": byte_count, "sha256": digest})
    return normalized


def _normalize_attribution(value: object, artifact_names: set[str]) -> dict[str, str]:
    item = _is_exact_dict(value, _ATTRIBUTION_FIELDS, "attribution")
    normalized: dict[str, str] = {}
    for field in sorted(_ATTRIBUTION_FIELDS):
        name = item[field]
        if not isinstance(name, str) or name not in artifact_names:
            raise DailyUseContractError("attribution artifact is unavailable")
        normalized[field] = name
    if len(set(normalized.values())) != len(normalized):
        raise DailyUseContractError("attribution artifacts are not distinct")
    return normalized


def _normalize_limitations(value: object) -> dict[str, list[str]]:
    item = _is_exact_dict(value, _LIMITATIONS_FIELDS, "limitations")
    entries = item["items"]
    if type(entries) is not list or len(entries) > MAX_LIMITATIONS:
        raise DailyUseContractError("limitations are outside their bound")
    normalized = [
        _reason(entry, "limitation", _LIMITATION_REASONS) for entry in entries
    ]
    if len(set(normalized)) != len(normalized):
        raise DailyUseContractError("limitations are not unique")
    return {"items": normalized}


def validate_daily_use_result(value: object) -> dict[str, object]:
    """Return one detached daily-use result after enforcing its closed schema."""

    result = _is_exact_dict(value, _TOP_LEVEL_FIELDS, "daily-use result")
    if type(result["schemaVersion"]) is not int or result["schemaVersion"] != 1:
        raise DailyUseContractError("daily-use schema version is invalid")
    run_id = result["runId"]
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise DailyUseContractError("daily-use run identity is invalid")
    identities = _is_exact_dict(result["identities"], _IDENTITIES_FIELDS, "identities")
    firefox = _normalize_process_identity(identities["firefox"], "Firefox identity")
    xorg = _normalize_process_identity(identities["xorg"], "Xorg identity")
    if firefox["initial"]["pid"] == xorg["initial"]["pid"]:
        raise DailyUseContractError("Firefox and Xorg process identities overlap")

    function_groups = _normalize_function_groups(result["functionGroups"])
    expected_state = (
        "pass" if all(item["state"] == "pass" for item in function_groups) else "fail"
    )
    if result["state"] != expected_state:
        raise DailyUseContractError("daily-use state disagrees with function groups")

    performance = _normalize_performance(result["performance"])
    slow_count = sum(item["state"] == "slow" for item in performance)
    if type(result["slowCount"]) is not int or result["slowCount"] != slow_count:
        raise DailyUseContractError("slow count is not derived from performance")

    artifacts = _normalize_artifacts(result["artifacts"])
    artifact_names = {str(item["name"]) for item in artifacts}
    attribution = _normalize_attribution(result["attribution"], artifact_names)
    limitations = _normalize_limitations(result["limitations"])
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "state": expected_state,
        "identities": {"firefox": firefox, "xorg": xorg},
        "functionGroups": function_groups,
        "performance": performance,
        "slowCount": slow_count,
        "artifacts": artifacts,
        "attribution": attribution,
        "limitations": limitations,
    }


def build_daily_use_result(
    *,
    run_id: object,
    firefox_identity: object,
    xorg_identity: object,
    function_groups: object,
    performance: object,
    artifacts: object,
    attribution: object,
    limitations: object,
) -> dict[str, object]:
    """Build and validate a daily-use result from its explicit components."""

    state = "pass"
    if not isinstance(function_groups, list) or any(
        not isinstance(item, dict) or item.get("state") != "pass"
        for item in function_groups
    ):
        state = "fail"
    slow_count = (
        sum(
            isinstance(item, dict) and item.get("state") == "slow"
            for item in performance
        )
        if isinstance(performance, list)
        else 0
    )
    return validate_daily_use_result(
        {
            "schemaVersion": 1,
            "runId": run_id,
            "state": state,
            "identities": {"firefox": firefox_identity, "xorg": xorg_identity},
            "functionGroups": function_groups,
            "performance": performance,
            "slowCount": slow_count,
            "artifacts": artifacts,
            "attribution": attribution,
            "limitations": limitations,
        }
    )
