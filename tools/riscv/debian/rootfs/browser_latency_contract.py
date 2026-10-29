#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate browser clock-domain latency samples without assuming scanout time."""

from __future__ import annotations

import math


KINDS = ("keyboard", "pointer", "scroll")
SOURCES = ("trusted", "synthetic")
MAX_SAMPLES_PER_KIND_SOURCE = 64
MAX_NAVIGATIONS = 16
MAX_DURATION_MS = 60_000.0


class BrowserLatencyError(ValueError):
    """Browser timing evidence is absent, incomplete, or inconsistent."""


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserLatencyError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= MAX_DURATION_MS:
        raise BrowserLatencyError(f"{label} is out of bounds")
    return result


def _summary(values: list[float]) -> dict[str, int | float]:
    if not values:
        raise BrowserLatencyError("latency samples are missing")
    ordered = sorted(values)
    p50 = max(1, math.ceil(0.50 * len(ordered))) - 1
    p95 = max(1, math.ceil(0.95 * len(ordered))) - 1
    return {
        "count": len(ordered),
        "min_ms": ordered[0],
        "p50_ms": ordered[p50],
        "p95_ms": ordered[p95],
        "max_ms": ordered[-1],
    }


def summarize_interactions(snapshot: object, *, source: str) -> dict[str, object]:
    """Separate trusted evdev-mediated interaction from synthetic JS scheduling."""

    if source not in SOURCES:
        raise BrowserLatencyError("interaction source is unsupported")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"schemaVersion", "clockDomain", "samples"}
        or type(snapshot["schemaVersion"]) is not int
        or snapshot["schemaVersion"] != 1
        or snapshot["clockDomain"] != "browser-performance-now"
        or not isinstance(snapshot["samples"], list)
        or not 1 <= len(snapshot["samples"]) <= len(KINDS) * len(SOURCES) * MAX_SAMPLES_PER_KIND_SOURCE
    ):
        raise BrowserLatencyError("interaction snapshot has malformed fields")

    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for entry in snapshot["samples"]:
        if not isinstance(entry, dict) or set(entry) != {
            "kind", "source", "firstRafMs", "nextRafMs"
        }:
            raise BrowserLatencyError("interaction sample has malformed fields")
        kind, entry_source = entry["kind"], entry["source"]
        if kind not in KINDS or entry_source not in SOURCES:
            raise BrowserLatencyError("interaction sample kind or source is unsupported")
        first = _number(entry["firstRafMs"], "first rAF")
        following = _number(entry["nextRafMs"], "next rAF")
        if following < first:
            raise BrowserLatencyError("rAF callbacks are reordered")
        key = (kind, entry_source)
        values = grouped.setdefault(key, [])
        values.append((first, following))
        if len(values) > MAX_SAMPLES_PER_KIND_SOURCE:
            raise BrowserLatencyError("interaction sample count exceeds its kind bound")

    result: dict[str, object] = {
        "clock_domain": "browser-performance-now",
        "source": source,
        "scope": "event-handler-to-rAF-callback; not physical framebuffer or HDMI",
    }
    for kind in KINDS:
        values = grouped.get((kind, source), [])
        if not values:
            raise BrowserLatencyError(f"{kind} samples are missing for {source}")
        result[kind] = {
            "first_raf_ms": _summary([first for first, _ in values]),
            "next_raf_ms": _summary([following for _, following in values]),
        }
    return result


NAVIGATION_FIELDS = (
    "startTime",
    "fetchStart",
    "responseStart",
    "responseEnd",
    "domContentLoadedEventEnd",
    "loadEventEnd",
)


def validate_navigation(snapshot: object) -> dict[str, float | str]:
    """Return disjoint local-page waterfall components in the browser domain."""

    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"schemaVersion", "clockDomain", *NAVIGATION_FIELDS}
        or type(snapshot["schemaVersion"]) is not int
        or snapshot["schemaVersion"] != 1
        or snapshot["clockDomain"] != "browser-navigation"
    ):
        raise BrowserLatencyError("navigation snapshot has malformed fields")
    times = [_number(snapshot[field], field) for field in NAVIGATION_FIELDS]
    if times[-1] == 0 or times != sorted(times):
        raise BrowserLatencyError("navigation is incomplete or reordered")
    start, fetch, first_byte, response_end, dom_end, load_end = times
    return {
        "clock_domain": "browser-navigation",
        "pre_fetch_ms": fetch - start,
        "request_to_first_byte_ms": first_byte - fetch,
        "body_transfer_ms": response_end - first_byte,
        "response_to_dom_ms": dom_end - response_end,
        "dom_to_load_ms": load_end - dom_end,
        "total_ms": load_end - start,
    }


def summarize_navigations(snapshots: object) -> dict[str, object]:
    """Summarize a bounded series of complete local-page navigations."""

    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= MAX_NAVIGATIONS:
        raise BrowserLatencyError("navigation sample count is out of bounds")
    parts = [validate_navigation(snapshot) for snapshot in snapshots]
    components = (
        "pre_fetch_ms",
        "request_to_first_byte_ms",
        "body_transfer_ms",
        "response_to_dom_ms",
        "dom_to_load_ms",
        "total_ms",
    )
    result: dict[str, object] = {"clock_domain": "browser-navigation"}
    for name in components:
        result[name] = _summary([float(item[name]) for item in parts])
    return result
