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
MAX_MONOTONIC_NS = 2**63 - 1

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

_PERFORMANCE_CLOCK_DOMAINS = {
    "startup": "guest-monotonic",
    "input": "browser-performance-now",
    "scroll": "browser-performance-now",
    "navigation": "multiple-clock-domains-separated",
    "context-switch": "guest-monotonic",
}
_STARTUP_METRICS_FIELDS = frozenset(
    {"firefoxPid", "bootFirefoxExecNs", "bootFirstWindowReadyNs", "durationMs"}
)
_INPUT_METRICS_FIELDS = frozenset({"keyboard", "pointer"})
_SCROLL_METRICS_FIELDS = frozenset({"firstRaf", "nextRaf"})
_RAF_SUMMARY_FIELDS = frozenset({"p50Ms", "p95Ms"})
_NAVIGATION_METRICS_FIELDS = frozenset({"localCommand", "browserNavigation"})
_LOCAL_COMMAND_FIELDS = frozenset({"clockDomain", "durationMs"})
_BROWSER_NAVIGATION_FIELDS = frozenset(
    {
        "clockDomain",
        "fetchStartMs",
        "fetchStartValid",
        "responseToDomMs",
        "responseToLoadMs",
    }
)
_CONTEXT_METRICS_FIELDS = frozenset(
    {
        "openMs",
        "selectMs",
        "returnMs",
        "closeMs",
        "totalMs",
        "handleCountBefore",
        "handleCountAfter",
    }
)
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


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_MONOTONIC_NS:
        raise DailyUseContractError(f"{label} is invalid")
    return value


def _signed_time_ms(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DailyUseContractError(f"{label} is not numeric")
    try:
        result = float(value)
    except OverflowError as error:
        raise DailyUseContractError(f"{label} is outside its bound") from error
    if not math.isfinite(result) or not -MAX_DURATION_MS <= result <= MAX_DURATION_MS:
        raise DailyUseContractError(f"{label} is outside its bound")
    return result


def _normalize_raf_summary(value: object, label: str) -> dict[str, float]:
    item = _is_exact_dict(value, _RAF_SUMMARY_FIELDS, label)
    p50 = _duration_ms(item["p50Ms"], f"{label} p50")
    p95 = _duration_ms(item["p95Ms"], f"{label} p95")
    if p50 > p95:
        raise DailyUseContractError(f"{label} percentiles are reordered")
    return {"p50Ms": p50, "p95Ms": p95}


def _normalize_navigation_metrics(value: object) -> dict[str, object]:
    metrics = _is_exact_dict(value, _NAVIGATION_METRICS_FIELDS, "navigation metrics")
    local = _is_exact_dict(metrics["localCommand"], _LOCAL_COMMAND_FIELDS, "local command")
    if local["clockDomain"] != "guest-monotonic":
        raise DailyUseContractError("local command clock domain is invalid")
    local_duration = _duration_ms(local["durationMs"], "local command duration")

    browser = _is_exact_dict(
        metrics["browserNavigation"],
        _BROWSER_NAVIGATION_FIELDS,
        "browser navigation",
    )
    if browser["clockDomain"] != "browser-navigation":
        raise DailyUseContractError("browser navigation clock domain is invalid")
    fetch_start = _signed_time_ms(browser["fetchStartMs"], "navigation fetch start")
    fetch_start_valid = browser["fetchStartValid"]
    if type(fetch_start_valid) is not bool or fetch_start_valid != (fetch_start >= 0):
        raise DailyUseContractError("navigation fetch-start validity is invalid")
    response_to_dom = browser["responseToDomMs"]
    response_to_load = browser["responseToLoadMs"]
    normalized_response_to_dom = _duration_ms(
        response_to_dom,
        "response to DOM duration",
    )
    normalized_response_to_load = _duration_ms(
        response_to_load,
        "response to load duration",
    )
    if normalized_response_to_load < normalized_response_to_dom:
        raise DailyUseContractError("navigation response intervals are reordered")
    return {
        "localCommand": {
            "clockDomain": "guest-monotonic",
            "durationMs": local_duration,
        },
        "browserNavigation": {
            "clockDomain": "browser-navigation",
            "fetchStartMs": fetch_start,
            "fetchStartValid": fetch_start_valid,
            "responseToDomMs": normalized_response_to_dom,
            "responseToLoadMs": normalized_response_to_load,
        },
    }


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
        if not isinstance(state, str) or state not in {"pass", "fail", "unsupported"}:
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


def _normalize_startup_metrics(
    value: object,
    firefox_pid: int,
) -> dict[str, int | float]:
    metrics = _is_exact_dict(value, _STARTUP_METRICS_FIELDS, "startup metrics")
    observed_pid = _positive_int(metrics["firefoxPid"], "startup Firefox PID")
    if observed_pid != firefox_pid:
        raise DailyUseContractError("startup Firefox identity changed")
    start = _nonnegative_int(metrics["bootFirefoxExecNs"], "Firefox execution time")
    end = _nonnegative_int(
        metrics["bootFirstWindowReadyNs"],
        "first Firefox window time",
    )
    if end < start:
        raise DailyUseContractError("startup endpoints are reordered")
    duration = _duration_ms(metrics["durationMs"], "startup duration")
    derived_duration = (end - start) / 1_000_000
    if duration != derived_duration:
        raise DailyUseContractError("startup duration is not derived from endpoints")
    return {
        "firefoxPid": observed_pid,
        "bootFirefoxExecNs": start,
        "bootFirstWindowReadyNs": end,
        "durationMs": derived_duration,
    }


def _normalize_input_metrics(value: object) -> tuple[dict[str, object], bool]:
    metrics = _is_exact_dict(value, _INPUT_METRICS_FIELDS, "input metrics")
    normalized: dict[str, object] = {}
    is_slow = False
    for device in ("keyboard", "pointer"):
        item = _is_exact_dict(metrics[device], _SCROLL_METRICS_FIELDS, device)
        first = _normalize_raf_summary(item["firstRaf"], f"{device} first rAF")
        following = _normalize_raf_summary(item["nextRaf"], f"{device} next rAF")
        normalized[device] = {"firstRaf": first, "nextRaf": following}
        is_slow |= max(first["p95Ms"], following["p95Ms"]) > 100.0
    return normalized, is_slow


def _normalize_scroll_metrics(value: object) -> tuple[dict[str, object], bool]:
    metrics = _is_exact_dict(value, _SCROLL_METRICS_FIELDS, "scroll metrics")
    first = _normalize_raf_summary(metrics["firstRaf"], "scroll first rAF")
    following = _normalize_raf_summary(metrics["nextRaf"], "scroll next rAF")
    return (
        {"firstRaf": first, "nextRaf": following},
        max(first["p95Ms"], following["p95Ms"]) > 100.0,
    )


def _normalize_context_metrics(value: object) -> tuple[dict[str, int | float], bool]:
    metrics = _is_exact_dict(value, _CONTEXT_METRICS_FIELDS, "context-switch metrics")
    durations = {
        name: _duration_ms(metrics[name], f"context switch {name}")
        for name in ("openMs", "selectMs", "returnMs", "closeMs")
    }
    total = _duration_ms(metrics["totalMs"], "context switch total")
    derived_total = sum(durations.values())
    if total != derived_total:
        raise DailyUseContractError("context switch total is not derived from operations")
    before = _nonnegative_int(metrics["handleCountBefore"], "context handle count")
    after = _nonnegative_int(metrics["handleCountAfter"], "context handle count")
    if before != 1 or after != 1:
        raise DailyUseContractError("context cleanup did not preserve one original handle")
    return (
        {
            **durations,
            "totalMs": derived_total,
            "handleCountBefore": before,
            "handleCountAfter": after,
        },
        max(*durations.values(), derived_total) > 500.0,
    )


def _normalize_performance(
    value: object,
    firefox_pid: int,
) -> list[dict[str, object]]:
    if type(value) is not list or len(value) != len(PERFORMANCE_CATEGORIES):
        raise DailyUseContractError("performance categories are missing or reordered")
    normalized: list[dict[str, object]] = []
    for expected_name, entry in zip(PERFORMANCE_CATEGORIES, value):
        item = _is_exact_dict(entry, _PERFORMANCE_FIELDS, "performance category")
        if item["name"] != expected_name:
            raise DailyUseContractError("performance categories are missing or reordered")
        clock_domain = _PERFORMANCE_CLOCK_DOMAINS[expected_name]
        if item["clockDomain"] != clock_domain:
            raise DailyUseContractError("performance category clock domain is invalid")
        state = item["state"]
        if not isinstance(state, str) or state not in {"pass", "slow", "unsupported"}:
            raise DailyUseContractError("performance category state is invalid")
        metrics = item["metrics"]
        reason = item["reason"]
        if state == "unsupported":
            normalized_reason: str | None = _reason(
                reason,
                "performance reason",
                _PERFORMANCE_REASONS,
            )
            if type(metrics) is not dict or metrics:
                raise DailyUseContractError("unsupported performance has metrics")
            normalized_metrics = {}
        else:
            if reason is not None:
                raise DailyUseContractError("measured performance has a reason")
            if expected_name == "startup":
                normalized_metrics = _normalize_startup_metrics(metrics, firefox_pid)
                is_slow = False
            elif expected_name == "input":
                normalized_metrics, is_slow = _normalize_input_metrics(metrics)
            elif expected_name == "scroll":
                normalized_metrics, is_slow = _normalize_scroll_metrics(metrics)
            elif expected_name == "navigation":
                normalized_metrics = _normalize_navigation_metrics(metrics)
                browser = normalized_metrics["browserNavigation"]
                assert isinstance(browser, dict)
                response_to_dom = browser["responseToDomMs"]
                assert isinstance(response_to_dom, float)
                is_slow = response_to_dom > 2_000.0
            else:
                normalized_metrics, is_slow = _normalize_context_metrics(metrics)
            if (state == "slow") != is_slow:
                raise DailyUseContractError("performance state disagrees with threshold")
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

    performance = _normalize_performance(result["performance"], firefox["initial"]["pid"])
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
