#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Orchestrate bounded daily-use captures in one existing Firefox process.

Adapters return bytes and contract values; only this module publishes evidence.
Sampler adapters must set ready after their first sample, keep sampling until
stop, and return their final sample's monotonic timestamp. Browser adapters must
bound their own I/O, honor the request deadline, and use the supplied
existing-session transport. Synchronous Python callbacks cannot be preempted;
the orchestrator checks their deadline before invocation and after return.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from typing import Protocol
from urllib.parse import urlparse

if Path("/run/asterinas-tools/browser_daily_use_contract.py").is_file():
    sys.path.insert(0, "/run/asterinas-tools")
    from browser_m5_marionette_gate import CommandNotSentTimeout  # type: ignore[import-not-found]
    from browser_daily_use_contract import (  # type: ignore[import-not-found]
        FUNCTION_GROUPS,
        MAX_ARTIFACT_BYTES,
        DailyUseContractError,
        _normalize_function_groups,
        build_daily_use_result,
    )
    from browser_perf_capture import (  # type: ignore[import-not-found]
        _connect,
        _process_starttimes,
        resolve_fixture_index_url,
    )
    import browser_composite_capture as composite_capture  # type: ignore[import-not-found]
    import browser_perf_capture as perf_capture  # type: ignore[import-not-found]
    import browser_system_time as system_time  # type: ignore[import-not-found]
    import browser_web_marionette_gate as web_gate  # type: ignore[import-not-found]
else:
    from tools.riscv.debian.rootfs.browser_m5_marionette_gate import (
        CommandNotSentTimeout,
    )
    from tools.riscv.debian.rootfs.browser_daily_use_contract import (
        FUNCTION_GROUPS,
        MAX_ARTIFACT_BYTES,
        DailyUseContractError,
        _normalize_function_groups,
        build_daily_use_result,
    )
    from tools.riscv.debian.rootfs.browser_perf_capture import (
        _connect,
        _process_starttimes,
        resolve_fixture_index_url,
    )
    from tools.riscv.debian.rootfs import (
        browser_composite_capture as composite_capture,
        browser_perf_capture as perf_capture,
        browser_system_time as system_time,
        browser_web_marionette_gate as web_gate,
    )


PHASES = (
    "session",
    "samplers-ready",
    "fixture",
    "local-timing",
    "context-switch",
    "composite",
    "samplers-stopped",
    "cleanup",
    "identities",
    "coverage",
    "artifacts",
    "validated",
)
ARTIFACT_NAMES = (
    "browser-fixture-capture.json",
    "browser-local-capture.json",
    "browser-context-switch.json",
    "browser-composite-capture.json",
    "browser-system-time.json",
    "browser-thread-time.json",
)
RESULT_NAME = "browser-daily-use-result.json"
CHECKPOINT_NAME = "browser-daily-use-checkpoint.json"
MAX_TIMEOUT_SECONDS = 120.0
PHYSICAL_SESSION_SETUP_TIMEOUT_SECONDS = 300.0
CONTEXT_CLEANUP_TIMEOUT_SECONDS = 5.0
FIXTURE_GROUPS = ("document", "storage", "execution", "rendering-media", "download")
_FIXTURE_CAPABILITY_CHECKS = frozenset(
    {
        "audio",
        "canvas",
        "cookie",
        "fetch",
        "indexedDb",
        "localStorage",
        "sessionStorage",
        "wasm",
        "worker",
    }
)
_STORAGE_CAPABILITY_CHECKS = ("localStorage", "sessionStorage", "cookie", "indexedDb")
_EXECUTION_CAPABILITY_CHECKS = ("wasm", "worker", "fetch")
_RENDERING_MEDIA_CAPABILITY_CHECKS = ("canvas", "audio")
STARTUP_TIMELINE = Path("/home/asterinas/browser-web-timeline.log")
_FAILURE_REASONS = frozenset(
    {
        "artifact-invalid",
        "cleanup-failed",
        "contract-invalid",
        "evidence-directory-invalid",
        "evidence-exists",
        "identity-changed",
        "identity-invalid",
        "parameters-invalid",
        "phase-failed",
        "phase-timeout",
        "phase-value-invalid",
        "sampler-coverage-invalid",
        "sampler-failed",
        "sampler-ready-timeout",
        "sampler-stop-timeout",
        "session-command-forbidden",
        "session-invalid",
    }
)


class DailyUseGateError(ValueError):
    """A canonical, bounded failure reason for an incomplete daily-use run."""


class BrowserTransport(Protocol):
    def command(self, name: str, parameters: dict | None = None) -> object: ...
    def set_timeout(self, timeout: float) -> None: ...
    def recover_timed_out_command(self, timeout: float) -> object: ...
    def close(self) -> None: ...


class ExistingSession:
    """Expose browser commands without session creation, deletion, or closure."""

    def __init__(self, client: BrowserTransport):
        self.__client = client
        self.__timed_out_command = False

    @property
    def has_timed_out_command(self) -> bool:
        """Whether a sent command reply must be drained before raw cleanup."""

        return self.__timed_out_command

    def command(self, name: str, parameters: dict | None = None) -> object:
        if name in {
            "WebDriver:NewSession",
            "WebDriver:DeleteSession",
            "Marionette:Quit",
        }:
            raise DailyUseGateError("session-command-forbidden")
        try:
            return self.__client.command(name, parameters)
        except CommandNotSentTimeout:
            # There is no outstanding response to drain before cleanup.
            raise
        except TimeoutError:
            self.__timed_out_command = True
            raise

    def recover_timed_out_command(self, timeout: float) -> None:
        """Synchronize any late response before reserving a bounded cleanup phase."""
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= CONTEXT_CLEANUP_TIMEOUT_SECONDS
        ):
            raise DailyUseGateError("parameters-invalid")
        if self.__timed_out_command:
            self.__client.recover_timed_out_command(timeout)
            self.__timed_out_command = False
        self.__client.set_timeout(timeout)


@dataclass(frozen=True)
class DailyUseClock:
    """The shared guest monotonic clock for deadlines and sample coverage."""

    monotonic: Callable[[], float] = time.monotonic
    monotonic_ns: Callable[[], int] = time.monotonic_ns


@dataclass(frozen=True)
class CaptureRequest:
    client: ExistingSession
    original_window: str
    fixture_index_url: str
    mode: str
    firefox_pid: int
    xorg_pid: int
    deadline: float
    clock: DailyUseClock
    run_id: str
    physical: bool = False


@dataclass(frozen=True)
class FixtureCapture:
    function_groups: list[dict[str, object]]
    artifact: bytes
    limitations: dict[str, object]


@dataclass(frozen=True)
class TimingCapture:
    performance: list[dict[str, object]]
    artifact: bytes
    function_group: dict[str, object]


@dataclass(frozen=True)
class ContextCapture:
    performance: dict[str, object]
    artifact: bytes
    function_group: dict[str, object]


@dataclass(frozen=True)
class CompositeCapture:
    artifact: bytes
    limitations: dict[str, object]


@dataclass(frozen=True)
class SamplerRequest:
    process_ids: tuple[int, int]
    mode: str
    ready: threading.Event
    stop: threading.Event
    deadline: float
    clock: DailyUseClock
    physical: bool = False


@dataclass(frozen=True)
class SamplerCapture:
    artifact: bytes
    first_sample_ns: int
    last_sample_ns: int


@dataclass(frozen=True)
class DailyUseOperations:
    fixture: Callable[[CaptureRequest], FixtureCapture]
    local_timing: Callable[[CaptureRequest], TimingCapture]
    context_switch: Callable[[CaptureRequest], ContextCapture]
    composite: Callable[[CaptureRequest], CompositeCapture]
    system_sampler: Callable[[SamplerRequest], SamplerCapture]
    thread_sampler: Callable[[SamplerRequest], SamplerCapture]
    identity_reader: Callable[[tuple[int, int]], tuple[int, int]] = _process_starttimes
    first_window_ready: Callable[[int], None] = lambda firefox_pid: None
    clock: DailyUseClock = field(default_factory=DailyUseClock)


def run_daily_use_gate(
    *,
    client: BrowserTransport,
    operations: DailyUseOperations,
    firefox_pid: int,
    xorg_pid: int,
    evidence_dir: Path,
    mode: str,
    timeout_seconds: float,
    run_id: str,
    fixture_index_url: str | None = None,
    physical: bool = False,
) -> dict[str, object]:
    """Publish a validated result, or a checkpoint and raise DailyUseGateError.

    Each browser phase and each sampler handshake gets the bounded timeout. Invalid
    parameters or occupied/unsafe evidence directories are rejected without writes.
    The private staging directory is retained as a run reservation on every exit.
    """
    completed: list[str] = []
    function_groups: list[dict[str, object]] = []
    published: list[Path] = []
    staging: Path | None = None
    original: str | None = None
    baseline_handles: tuple[str, ...] | None = None
    phase_session: ExistingSession | None = None
    samplers: list[_Sampler] = []
    cleaned = False
    transport_closed = False
    clock = operations.clock
    try:
        if type(physical) is not bool:
            raise DailyUseGateError("parameters-invalid")
        fixture_url = _validate_inputs(
            firefox_pid,
            xorg_pid,
            evidence_dir,
            mode,
            timeout_seconds,
            run_id,
            fixture_index_url,
        )
        staging = _reserve_evidence(evidence_dir, run_id)
        pids = (firefox_pid, xorg_pid)
        initial = _identities(operations, pids)
        client.set_timeout(
            PHYSICAL_SESSION_SETUP_TIMEOUT_SECONDS if physical else timeout_seconds
        )
        session = _value(
            client.command(
                "WebDriver:NewSession",
                {
                    "acceptInsecureCerts": False,
                    "pageLoadStrategy": "none",
                    "strictFileInteractability": True,
                },
            )
        )
        baseline = _value(client.command("WebDriver:GetWindowHandles"))
        original = _value(client.command("WebDriver:GetWindowHandle"))
        if (
            type(baseline) is not list
            or len(baseline) != 1
            or any(type(handle) is not str or not handle for handle in baseline)
            or not isinstance(original, str)
            or not original
            or baseline != [original]
        ):
            original = None
            raise DailyUseGateError("session-invalid")
        baseline_handles = tuple(baseline)
        _select(client, original)
        if (
            not isinstance(session, dict)
            or not isinstance(session.get("sessionId"), str)
            or not session["sessionId"]
            or not isinstance(session.get("capabilities"), dict)
            or session["capabilities"].get("acceptInsecureCerts") is not False
        ):
            raise DailyUseGateError("session-invalid")
        # The normal evidence service owns this endpoint on QEMU and public
        # web runs. Physical daily-use mode masks that service, so this gate
        # must record the endpoint it has just validated itself.
        if physical:
            operations.first_window_ready(firefox_pid)
        completed.append("session")

        # Sampling covers readiness and four phases; stop wakes the final sample.
        sampler_deadline = clock.monotonic() + 5 * timeout_seconds
        for name, operation in (
            ("system", operations.system_sampler),
            ("thread", operations.thread_sampler),
        ):
            sampler = _Sampler(
                name,
                operation,
                SamplerRequest(
                    pids,
                    mode,
                    threading.Event(),
                    threading.Event(),
                    sampler_deadline,
                    clock,
                    physical,
                ),
            )
            samplers.append(sampler)
            sampler.start()
        _wait_ready(samplers, timeout_seconds, clock)
        completed.append("samplers-ready")
        workload_start_ns = clock.monotonic_ns()

        captures = []
        for phase, operation, expected in (
            ("fixture", operations.fixture, FixtureCapture),
            ("local-timing", operations.local_timing, TimingCapture),
            ("context-switch", operations.context_switch, ContextCapture),
            ("composite", operations.composite, CompositeCapture),
        ):
            _check_running(samplers)
            client.set_timeout(timeout_seconds)
            phase_session = ExistingSession(client)
            request = CaptureRequest(
                phase_session,
                original,
                fixture_url,
                mode,
                firefox_pid,
                xorg_pid,
                clock.monotonic() + timeout_seconds,
                clock,
                run_id,
                physical,
            )
            if clock.monotonic() > request.deadline:
                raise DailyUseGateError("phase-timeout")
            capture = operation(request)
            if clock.monotonic() > request.deadline:
                raise DailyUseGateError("phase-timeout")
            _check_running(samplers)
            if type(capture) is not expected:
                raise DailyUseGateError("phase-value-invalid")
            _artifact_bytes(capture.artifact)
            if type(capture) is FixtureCapture:
                function_groups.extend(
                    _owned_groups(capture.function_groups, FIXTURE_GROUPS)
                )
            elif type(capture) is TimingCapture:
                function_groups.extend(
                    _owned_groups([capture.function_group], ("navigation",))
                )
            elif type(capture) is ContextCapture:
                function_groups.extend(
                    _owned_groups([capture.function_group], ("contexts",))
                )
            function_groups.sort(key=lambda item: FUNCTION_GROUPS.index(item["name"]))
            captures.append(capture)
            completed.append(phase)
        workload_end_ns = clock.monotonic_ns()
        _stop_samplers(samplers, timeout_seconds, clock)
        completed.append("samplers-stopped")
        _cleanup(client, original, baseline_handles, timeout_seconds)
        cleaned = True
        completed.append("cleanup")
        final = _identities(operations, pids)
        if initial != final:
            raise DailyUseGateError("identity-changed")
        completed.append("identities")

        samples = [sampler.result for sampler in samplers]
        for sample in samples:
            if type(sample) is not SamplerCapture:
                raise DailyUseGateError("sampler-failed")
            _artifact_bytes(sample.artifact)
            if (
                type(sample.first_sample_ns) is not int
                or type(sample.last_sample_ns) is not int
                or not 0 < sample.first_sample_ns <= workload_start_ns
                or not workload_end_ns <= sample.last_sample_ns <= clock.monotonic_ns()
            ):
                raise DailyUseGateError("sampler-coverage-invalid")
        completed.append("coverage")
        artifacts = []
        for name, capture in zip(ARTIFACT_NAMES, captures + samples):
            payload = _artifact_bytes(capture.artifact)
            _write_private(staging / name, payload)
            artifacts.append(
                {
                    "name": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        completed.append("artifacts")
        fixture, timing, context, composite = captures
        result = build_daily_use_result(
            run_id=run_id,
            firefox_identity={"initial": initial[0], "final": final[0]},
            xorg_identity={"initial": initial[1], "final": final[1]},
            function_groups=function_groups,
            performance=timing.performance + [context.performance],
            artifacts=artifacts,
            attribution={
                "compositeArtifact": ARTIFACT_NAMES[3],
                "systemArtifact": ARTIFACT_NAMES[4],
                "threadArtifact": ARTIFACT_NAMES[5],
            },
            limitations=_merge_limitations(
                fixture.limitations, composite.limitations
            ),
        )
        completed.append("validated")
        if result["state"] != "pass":
            raise DailyUseGateError("phase-failed")
        client.close()
        transport_closed = True
        for name in ARTIFACT_NAMES:
            _publish(staging / name, evidence_dir / name)
            published.append(evidence_dir / name)
        _write_private(staging / RESULT_NAME, _json_bytes(result))
        _publish(staging / RESULT_NAME, evidence_dir / RESULT_NAME)
        published.append(evidence_dir / RESULT_NAME)
        return result
    except BaseException as error:
        reason = (
            str(error)
            if type(error) is DailyUseGateError and str(error) in _FAILURE_REASONS
            else "contract-invalid"
            if isinstance(error, DailyUseContractError)
            else "phase-timeout"
            if isinstance(error, TimeoutError)
            else "phase-failed"
        )
        try:
            _stop_samplers(samplers, timeout_seconds, clock)
        except Exception:
            # Keep the triggering failure; an incomplete sampler cannot publish.
            pass
        cleanup_allowed = True
        if phase_session is not None and phase_session.has_timed_out_command:
            try:
                phase_session.recover_timed_out_command(
                    CONTEXT_CLEANUP_TIMEOUT_SECONDS
                )
            except Exception:
                reason = "cleanup-failed"
                cleanup_allowed = False
        if original is not None and not cleaned and cleanup_allowed:
            try:
                _cleanup(client, original, baseline_handles, timeout_seconds)
            except Exception:
                reason = "cleanup-failed"
        if not transport_closed:
            try:
                client.close()
                transport_closed = True
            except Exception:
                reason = "cleanup-failed"
        if staging is not None:
            for destination in reversed(published):
                try:
                    destination.unlink()
                except OSError:
                    # Persistence failure must not hide the canonical trigger.
                    pass
            try:
                checkpoint = {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "completedPhases": completed,
                    "functionGroups": function_groups,
                    "failure": {"type": "daily-use-gate", "reason": reason},
                }
                _write_private(staging / CHECKPOINT_NAME, _json_bytes(checkpoint))
                _publish(staging / CHECKPOINT_NAME, evidence_dir / CHECKPOINT_NAME)
            except (OSError, DailyUseGateError, DailyUseContractError):
                # No checkpoint is promised if evidence persistence is broken.
                pass
        raise DailyUseGateError(reason) from error


def default_operations(
    *,
    timeline_path: Path = STARTUP_TIMELINE,
    timeline_reader: Callable[[Path], str] | None = None,
    download_path: Path = web_gate.FIXTURE_DOWNLOAD_FILE,
    firefox_uid_reader: Callable[[int], int] = web_gate.firefox_process_uid,
    clock: DailyUseClock | None = None,
    context_cpu_diagnostic: bool = False,
) -> DailyUseOperations:
    """Adapt the existing local Firefox captures without granting publication access."""

    clock = clock or DailyUseClock()

    def fixture(request: CaptureRequest) -> FixtureCapture:
        return _capture_fixture(request, download_path, firefox_uid_reader)

    def timing(request: CaptureRequest) -> TimingCapture:
        startup = _startup_performance(
            (timeline_reader or _read_timeline)(timeline_path), request.firefox_pid
        )
        form_navigation = _capture_form_navigation(request)
        report = perf_capture.capture_local(
            request.client,
            resolve_fixture_index_url(request.fixture_index_url),
            synthetic_samples=1 if request.mode == "smoke" else 4,
            timeout_seconds=_remaining(request),
            navigation_validator=_navigation_parts,
        )
        summary = report["interaction_summary"]
        keyboard, pointer, scroll = (
            _raf_metrics(summary[name]) for name in ("keyboard", "pointer", "scroll")
        )
        performance = [
            startup,
            _performance(
                "input",
                "browser-performance-now",
                {"keyboard": keyboard, "pointer": pointer},
                slow=any(
                    item[phase]["p95Ms"] > 100
                    for item in (keyboard, pointer)
                    for phase in ("firstRaf", "nextRaf")
                ),
            ),
            _performance(
                "scroll",
                "browser-performance-now",
                scroll,
                slow=any(
                    scroll[phase]["p95Ms"] > 100 for phase in ("firstRaf", "nextRaf")
                ),
            ),
            _navigation_performance(report),
        ]
        report["startup"] = startup
        report["form_navigation"] = form_navigation
        return TimingCapture(
            performance, _json_bytes(report), _passing_group("navigation")
        )

    def composite(request: CaptureRequest) -> CompositeCapture:
        report = composite_capture.capture_composite(
            request.client,
            resolve_fixture_index_url(request.fixture_index_url),
            mode=request.mode,
            run_id=request.run_id,
            timeout_seconds=min(
                _remaining(request),
                composite_capture.MODES[request.mode]["deadline_seconds"],
            ),
            clock_ns=request.clock.monotonic_ns,
        )
        return CompositeCapture(
            _json_bytes(report),
            {
                "items": [
                    "guest-and-browser-clocks-separated",
                    "kernel-diagnostics-unavailable",
                    "physical-scanout-unsupported",
                    "public-network-excluded",
                    "synthetic-input-timing",
                ]
            },
        )

    return DailyUseOperations(
        fixture=fixture,
        local_timing=timing,
        context_switch=lambda request: _capture_context(
            request, cpu_diagnostic=context_cpu_diagnostic
        ),
        composite=composite,
        system_sampler=lambda request: _capture_sampler(request, threads=False),
        thread_sampler=lambda request: _capture_sampler(request, threads=True),
        first_window_ready=lambda firefox_pid: _record_first_window_ready(
            timeline_path, firefox_pid, clock
        ),
        clock=clock,
    )


def _remaining(request: CaptureRequest | SamplerRequest) -> float:
    remaining = request.deadline - request.clock.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise DailyUseGateError("phase-timeout")
    return remaining


def _passing_group(name: str) -> dict[str, object]:
    return {"name": name, "state": "pass", "reason": None}


def _fixture_group(
    name: str, checks: dict[str, bool], owned_checks: tuple[str, ...], *, required: bool
) -> dict[str, object]:
    if all(checks[item] for item in owned_checks):
        return _passing_group(name)
    return {
        "name": name,
        "state": "fail" if required else "unsupported",
        "reason": (
            "fixture-capability-failed"
            if required
            else "fixture-capability-unavailable"
        ),
    }


def _classify_daily_use_fixture(
    probe: object, expected_url: str
) -> tuple[list[dict[str, object]], dict[str, object]]:
    result = web_gate._probe_mapping(probe)
    if (
        result["url"] != expected_url
        or result["title"] != "Asterinas Browser Quality"
        or result["readyState"] != "complete"
        or result["jsComplete"] is not True
    ):
        raise web_gate.GateError("fixture home document or JavaScript is incomplete")
    body = result["bodyText"]
    if not isinstance(body, str) or not all(
        token in body for token in ("Asterinas browser quality", "浏览器质量")
    ):
        raise web_gate.GateError("fixture home lost its exact Latin/CJK content")
    dom = result["dom"]
    if not isinstance(dom, dict) or not all(
        dom.get(name) is True
        for name in ("fixtureQuery", "fixtureImage", "fixtureSecond")
    ):
        raise web_gate.GateError(
            "fixture home form, PNG, or navigation link is not ready"
        )

    capabilities = result["browserCapabilities"]
    if type(capabilities) is not dict or set(capabilities) != {
        "version",
        "phase",
        "state",
        "checks",
        "error",
    }:
        raise web_gate.GateError("fixture browser capability evidence is malformed")
    checks = capabilities["checks"]
    if (
        type(capabilities["version"]) is not int
        or capabilities["version"] != 1
        or capabilities["phase"] != "home"
        or type(checks) is not dict
        or set(checks) != _FIXTURE_CAPABILITY_CHECKS
        or any(type(value) is not bool for value in checks.values())
    ):
        raise web_gate.GateError("fixture browser capability evidence is malformed")
    state = capabilities["state"]
    error = capabilities["error"]
    all_checks_pass = all(checks.values())
    if state == "complete":
        if error is not None or not all_checks_pass:
            raise web_gate.GateError(
                "fixture browser capability evidence is inconsistent"
            )
    elif state == "error":
        if (
            all_checks_pass
            or not isinstance(error, str)
            or not error
            or len(error) > 160
        ):
            raise web_gate.GateError(
                "fixture browser capability evidence is inconsistent"
            )
    else:
        raise web_gate.GateError("fixture browser capabilities are not terminal")

    typed_checks = {name: checks[name] for name in _FIXTURE_CAPABILITY_CHECKS}
    groups = [
        _passing_group("document"),
        _fixture_group(
            "storage", typed_checks, _STORAGE_CAPABILITY_CHECKS, required=True
        ),
        _fixture_group(
            "execution", typed_checks, _EXECUTION_CAPABILITY_CHECKS, required=False
        ),
        _fixture_group(
            "rendering-media",
            typed_checks,
            _RENDERING_MEDIA_CAPABILITY_CHECKS,
            required=False,
        ),
    ]
    optional_unsupported = any(
        item["state"] == "unsupported" for item in groups
    )
    return (
        groups,
        {
            "items": (
                ["fixture-capabilities-incomplete"]
                if optional_unsupported
                else []
            )
        },
    )


def _merge_limitations(*values: object) -> dict[str, object]:
    combined: set[str] = set()
    for value in values:
        if type(value) is not dict or set(value) != {"items"}:
            raise DailyUseContractError("phase limitations are malformed")
        items = value["items"]
        if (
            type(items) is not list
            or any(not isinstance(item, str) for item in items)
            or len(set(items)) != len(items)
        ):
            raise DailyUseContractError("phase limitations are malformed")
        combined.update(items)
    return {"items": sorted(combined)}


def _owned_groups(entries, names):
    if (
        type(entries) is not list
        or len(entries) != len(names)
        or any(
            type(entry) is not dict or entry.get("name") != name
            for entry, name in zip(entries, names)
        )
    ):
        raise DailyUseContractError("phase function groups are missing or reordered")
    supplied = dict(zip(names, entries))
    # Use the contract's closed-schema validator; placeholders never leave this
    # validation call and only the phase's own, validated verdicts are returned.
    normalized = _normalize_function_groups(
        [
            supplied[name] if name in supplied else _passing_group(name)
            for name in FUNCTION_GROUPS
        ]
    )
    return [item for item in normalized if item["name"] in names]


def _capture_fixture(request, download_path, uid_reader):
    url = resolve_fixture_index_url(request.fixture_index_url)
    owner_uid = uid_reader(request.firefox_pid)
    if type(owner_uid) is not int or owner_uid < 0:
        raise DailyUseGateError("identity-invalid")
    _remove_prior_fixture_download(download_path, owner_uid)
    _remaining(request)
    web_gate._navigate(request.client, url)
    probe, classified = web_gate._wait_for_probe(
        request.client,
        lambda value: _classify_daily_use_fixture(value, url),
        request.deadline,
        diagnostic_fn=lambda line: None,
    )
    probe_groups, probe_limitations = classified
    snapshot = web_gate._snapshot(request.client)
    snapshot_groups, snapshot_limitations = _classify_daily_use_fixture(
        {
            name: snapshot[name]
            for name in (
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            )
        },
        url,
    )
    if (snapshot_groups, snapshot_limitations) != (
        probe_groups,
        probe_limitations,
    ):
        raise web_gate.GateError("fixture capability evidence changed during capture")
    _validate_fixture_resources(snapshot, url)
    # Both scripts use relative fixture paths. Search/resource validators in the
    # public-web gate deliberately accept slirp only and cannot serve the board.
    triggered = perf_capture._script(
        request.client,
        "if (document.URL !== arguments[0]) return 'unexpected-url';\n"
        + web_gate._FIXTURE_DOWNLOAD_SCRIPT,
        [url],
    )
    if triggered != "fixture-download-scheduled":
        raise DailyUseGateError("phase-value-invalid")
    with tempfile.TemporaryDirectory(prefix="browser-daily-download-") as directory:
        download = web_gate._wait_for_fixture_download(
            download_path, request.deadline, Path(directory), owner_uid
        )
    return FixtureCapture(
        probe_groups + [_passing_group("download")],
        _json_bytes({"probe": probe, "snapshot": snapshot, "download": download}),
        probe_limitations,
    )


def _remove_prior_fixture_download(path: Path, owner_uid: int) -> None:
    """Clear only the exact test-owned download left by an earlier valid run."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_size != web_gate.FIXTURE_DOWNLOAD_BYTES
    ):
        raise DailyUseGateError("phase-value-invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise DailyUseGateError("phase-value-invalid")
            digest = hashlib.sha256(stream.read(web_gate.FIXTURE_DOWNLOAD_BYTES + 1))
        current = path.lstat()
        if (
            digest.hexdigest() != web_gate.FIXTURE_DOWNLOAD_SHA256
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise DailyUseGateError("phase-value-invalid")
        path.unlink()
    except OSError as error:
        raise DailyUseGateError("phase-value-invalid") from error


def _validate_fixture_resources(snapshot, url):
    navigation = snapshot["navigation"]
    if (
        type(navigation) is not dict
        or navigation.get("name") != url
        or navigation.get("entryType") != "navigation"
    ):
        raise DailyUseGateError("phase-value-invalid")
    resources = snapshot["resources"]
    if type(resources) is not list or not 0 < len(resources) <= web_gate.MAX_RESOURCES:
        raise DailyUseGateError("phase-value-invalid")
    origin = urlparse(url)
    image_url = f"http://{origin.hostname}:17894{web_gate.FIXTURE_IMAGE_PATH}"
    image_seen = False
    for resource in resources:
        if (
            type(resource) is not dict
            or set(resource) != {"name", "initiatorType", "duration", "transferSize"}
            or not isinstance(resource["name"], str)
        ):
            raise DailyUseGateError("phase-value-invalid")
        parsed = urlparse(resource["name"])
        if (
            parsed.scheme != "http"
            or parsed.hostname != origin.hostname
            or parsed.port != 17894
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise DailyUseGateError("phase-value-invalid")
        image_seen |= resource["name"] == image_url
    if not image_seen:
        raise DailyUseGateError("phase-value-invalid")


def _capture_form_navigation(request: CaptureRequest) -> dict[str, object]:
    url = resolve_fixture_index_url(request.fixture_index_url)
    _remaining(request)
    web_gate._navigate(request.client, url)
    web_gate._wait_for_probe(
        request.client,
        lambda probe: web_gate.probe_fixture_home(probe, url),
        request.deadline,
        diagnostic_fn=lambda line: None,
    )
    web_gate._submit_fixture_search(request.client)
    search_url = url + "?q=asterinas"
    probe, _ = web_gate._wait_for_probe(
        request.client,
        lambda probe: _validate_form_destination(probe, search_url),
        request.deadline,
        diagnostic_fn=lambda line: None,
    )
    snapshot = web_gate._snapshot(request.client)
    _validate_form_destination(
        {name: snapshot[name] for name in probe if name != "apiTypes"}, search_url
    )
    _validate_fixture_resources(snapshot, search_url)
    return {"probe": probe, "snapshot": snapshot}


def _validate_form_destination(probe, expected_url):
    # The existing full search validator intentionally accepts only slirp.
    # Its content checks also apply to the board's already-validated origin.
    result = web_gate._probe_mapping(probe)
    if (
        result["url"] != expected_url
        or result["title"] != "asterinas - Asterinas Browser Quality"
        or result["readyState"] != "complete"
        or result["jsComplete"] is not True
        or not isinstance(result["bodyText"], str)
        or not all(
            token in result["bodyText"]
            for token in ("Asterinas browser quality", "浏览器质量")
        )
        or not all(
            result["dom"][name] is True
            for name in ("fixtureQuery", "fixtureImage", "fixtureSecond")
        )
    ):
        raise web_gate.GateError("local fixture form destination is incomplete")
    web_gate._validate_fixture_capabilities(result["browserCapabilities"], "search")


def _read_timeline(path: Path) -> str:
    with path.open(encoding="utf-8") as stream:
        return stream.read(64 * 1024 + 1)


def _record_first_window_ready(
    path: Path, firefox_pid: int, clock: DailyUseClock
) -> None:
    """Append the readiness endpoint owned by this one-session daily-use gate."""
    if type(firefox_pid) is not int or firefox_pid <= 1:
        raise DailyUseGateError("identity-invalid")
    descriptor = None
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
            raise DailyUseGateError("phase-value-invalid")
        payload = os.pread(descriptor, 64 * 1024 + 1, 0)
        raw = payload.decode("utf-8")
        ready_ns = clock.monotonic_ns()
        if type(ready_ns) is not int or ready_ns <= 0:
            raise DailyUseGateError("phase-value-invalid")
        line = (
            "A_WEB_TIMELINE marker=BOOT_FIRST_WINDOW_READY "
            f"guest_monotonic_ns={ready_ns} firefox_pid={firefox_pid}\n"
        )
        # Validate the complete startup interval before mutating the evidence
        # file. This also rejects a duplicate endpoint from another gate.
        _startup_performance(raw + line, firefox_pid)
        if os.write(descriptor, line.encode()) != len(line):
            raise DailyUseGateError("phase-value-invalid")
    except (OSError, UnicodeError) as error:
        raise DailyUseGateError("phase-value-invalid") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _startup_performance(raw, firefox_pid):
    if type(raw) is not str or len(raw) > 64 * 1024:
        raise DailyUseGateError("phase-value-invalid")
    records = []
    for line_index, line in enumerate(raw.splitlines()):
        if not re.search(
            r"(?:^|\s)marker=BOOT_(?:FIREFOX_EXEC|FIRST_WINDOW_READY)(?:\s|$)",
            line,
        ):
            continue
        match = re.fullmatch(
            r"A_WEB_TIMELINE marker=(BOOT_FIREFOX_EXEC|BOOT_FIRST_WINDOW_READY) "
            r"guest_monotonic_ns=([0-9]+) firefox_pid=([0-9]+)",
            line,
        )
        if match is None:
            raise DailyUseGateError("phase-value-invalid")
        records.append((match[1], int(match[2]), int(match[3]), line_index))
    execution = [record for record in records if record[0] == "BOOT_FIREFOX_EXEC"]
    ready = [record for record in records if record[0] == "BOOT_FIRST_WINDOW_READY"]
    if (
        len(execution) != 1
        or len(ready) != 1
        or execution[0][2] != firefox_pid
        or ready[0][2] != firefox_pid
        or not 0 < execution[0][1] < ready[0][1]
        or execution[0][3] >= ready[0][3]
    ):
        raise DailyUseGateError("phase-value-invalid")
    return _performance(
        "startup",
        "guest-monotonic",
        {
            "firefoxPid": firefox_pid,
            "bootFirefoxExecNs": execution[0][1],
            "bootFirstWindowReadyNs": ready[0][1],
            "durationMs": (ready[0][1] - execution[0][1]) / 1_000_000,
        },
    )


def _performance(name, domain, metrics, *, slow=False):
    return {
        "name": name,
        "state": "slow" if slow else "pass",
        "clockDomain": domain,
        "metrics": metrics,
        "reason": None,
    }


def _raf_metrics(summary):
    return {
        target: {"p50Ms": summary[source]["p50_ms"], "p95Ms": summary[source]["p95_ms"]}
        for target, source in (("firstRaf", "first_raf_ms"), ("nextRaf", "next_raf_ms"))
    }


def _navigation_parts(snapshot: object) -> dict[str, float | str]:
    if (
        type(snapshot) is not dict
        or set(snapshot)
        != {"schemaVersion", "clockDomain", *perf_capture.NAVIGATION_FIELDS}
        or type(snapshot["schemaVersion"]) is not int
        or snapshot["schemaVersion"] != 1
        or snapshot["clockDomain"] != "browser-navigation"
    ):
        raise DailyUseGateError("phase-value-invalid")
    numbers = [
        snapshot[name]
        for name in (
            "fetchStart",
            "responseEnd",
            "domContentLoadedEventEnd",
            "loadEventEnd",
        )
    ]
    if any(
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not -3_600_000 <= value <= 3_600_000
        for value in numbers
    ):
        return {"state": "unsupported", "reason": "navigation-timing-invalid"}
    fetch, response, dom, load = numbers
    if not 0 < response <= dom <= load:
        return {"state": "unsupported", "reason": "navigation-timing-invalid"}
    return {
        "clock_domain": "browser-navigation",
        "fetch_start_ms": fetch,
        "response_to_dom_ms": dom - response,
        "response_to_load_ms": load - response,
    }


def _navigation_performance(report):
    parts = _navigation_parts(report["navigation_snapshot"])
    if parts.get("state") == "unsupported":
        return {
            "name": "navigation",
            "state": "unsupported",
            "metrics": {},
            "clockDomain": "multiple-clock-domains-separated",
            "reason": parts["reason"],
        }
    start, end = report["second_navigation_command_ns"]
    if type(start) is not int or type(end) is not int or not 0 < start <= end:
        raise DailyUseGateError("phase-value-invalid")
    return _performance(
        "navigation",
        "multiple-clock-domains-separated",
        {
            "localCommand": {
                "clockDomain": "guest-monotonic",
                "durationMs": (end - start) / 1_000_000,
            },
            "browserNavigation": {
                "clockDomain": "browser-navigation",
                "fetchStartMs": parts["fetch_start_ms"],
                "fetchStartValid": parts["fetch_start_ms"] >= 0,
                "responseToDomMs": parts["response_to_dom_ms"],
                "responseToLoadMs": parts["response_to_load_ms"],
            },
        },
        slow=parts["response_to_dom_ms"] > 2_000,
    )


def _capture_context(
    request: CaptureRequest, *, cpu_diagnostic: bool = False
) -> ContextCapture:
    url = resolve_fixture_index_url(request.fixture_index_url)
    _remaining(request)
    client, original = request.client, request.original_window
    if (
        _value(client.command("WebDriver:GetWindowHandles")) != [original]
        or _value(client.command("WebDriver:GetWindowHandle")) != original
    ):
        raise DailyUseGateError("session-invalid")
    metrics = {"handleCountBefore": 1}

    def measure(name, operation):
        start = request.clock.monotonic_ns()
        result = operation()
        end = request.clock.monotonic_ns()
        if not 0 < start <= end:
            raise DailyUseGateError("phase-value-invalid")
        metrics[name] = (end - start) / 1_000_000
        return result

    open_cpu = None
    try:
        if cpu_diagnostic:
            before_read_ns = request.clock.monotonic_ns()
            before = system_time.read_snapshot(
                Path("/proc"),
                (request.firefox_pid, request.xorg_pid),
                before_read_ns,
            )
            before_read_end_ns = request.clock.monotonic_ns()
        window = _value(
            measure(
                "openMs", lambda: client.command("WebDriver:NewWindow", {"type": "tab"})
            )
        )
        if cpu_diagnostic:
            after_read_ns = request.clock.monotonic_ns()
            after = system_time.read_snapshot(
                Path("/proc"),
                (request.firefox_pid, request.xorg_pid),
                after_read_ns,
            )
            after_read_end_ns = request.clock.monotonic_ns()
            open_cpu = {
                "interval": system_time.interval(
                    before,
                    after,
                    clock_ticks_per_second=os.sysconf("SC_CLK_TCK"),
                ),
                "processScope": "firefox-parent-and-xorg",
                "readOverheadMs": (
                    before_read_end_ns - before_read_ns
                    + after_read_end_ns - after_read_ns
                ) / 1_000_000,
            }
        if (
            type(window) is not dict
            or type(window.get("handle")) is not str
            or not window["handle"]
            or window["handle"] == original
        ):
            raise DailyUseGateError("session-invalid")
        second = window["handle"]
        handles = _value(client.command("WebDriver:GetWindowHandles"))
        if (
            type(handles) is not list
            or len(handles) != 2
            or set(handles) != {original, second}
        ):
            raise DailyUseGateError("session-invalid")
        measure("selectMs", lambda: _select(client, second))
        if _value(client.command("WebDriver:GetWindowHandle")) != second:
            raise DailyUseGateError("session-invalid")
        web_gate._navigate(client, url)
        perf_capture._wait_document(client, url, request.deadline)
        measure("returnMs", lambda: _select(client, original))
        if _value(client.command("WebDriver:GetWindowHandle")) != original:
            raise DailyUseGateError("session-invalid")
    finally:
        # No cleanup command may consume the timed-out operation's late reply.
        # Recovery drains only that response; it cannot create another session.
        client.recover_timed_out_command(CONTEXT_CLEANUP_TIMEOUT_SECONDS)
        try:

            def close_extra():
                handles = _value(client.command("WebDriver:GetWindowHandles"))
                if type(handles) is not list or original not in handles:
                    raise DailyUseGateError("cleanup-failed")
                for handle in handles:
                    if handle != original:
                        _select(client, handle)
                        client.command("WebDriver:CloseWindow")

            measure("closeMs", close_extra)
        finally:
            _select(client, original)
    if (
        _value(client.command("WebDriver:GetWindowHandles")) != [original]
        or _value(client.command("WebDriver:GetWindowHandle")) != original
    ):
        raise DailyUseGateError("cleanup-failed")
    metrics["handleCountAfter"] = 1
    _remaining(request)
    durations = [metrics[key] for key in ("openMs", "selectMs", "returnMs", "closeMs")]
    metrics["totalMs"] = sum(durations)
    performance = _performance(
        "context-switch",
        "guest-monotonic",
        metrics,
        slow=max(*durations, metrics["totalMs"]) > 500,
    )
    artifact = (
        {"performance": performance, "openCpu": open_cpu}
        if cpu_diagnostic
        else performance
    )
    return ContextCapture(performance, _json_bytes(artifact), _passing_group("contexts"))


def _capture_sampler(request: SamplerRequest, *, threads: bool) -> SamplerCapture:
    remaining = _remaining(request)
    samples = min(system_time.MAX_INTERVALS, system_time.MAX_THREAD_INTERVALS)
    interval = max(system_time.MIN_INTERVAL_SECONDS, remaining / (samples - 1))
    if interval > system_time.MAX_INTERVAL_SECONDS:
        raise DailyUseGateError("parameters-invalid")
    with tempfile.TemporaryDirectory(prefix="browser-daily-sampler-") as directory:
        output = Path(directory) / "capture.json"
        kwargs = dict(
            interval_seconds=interval,
            samples=samples,
            clock_ns=request.clock.monotonic_ns,
            ready_fn=request.ready.set,
            stop_event=request.stop,
        )
        if threads:
            ready_marker = Path(directory) / "ready"
            _write_private(ready_marker, str(request.clock.monotonic_ns()).encode())
            system_time.run_thread_sampler(
                Path("/proc"),
                request.process_ids[0],
                ready_marker,
                output,
                physical=request.physical,
                **kwargs,
            )
        else:
            system_time.run_sampler(
                Path("/proc"), request.process_ids, output, **kwargs
            )
        payload = _artifact_bytes(output.read_bytes())
        intervals = json.loads(payload)["intervals"]
        if (
            type(intervals) is not list
            or not intervals
            or not request.ready.is_set()
            or not request.stop.is_set()
        ):
            raise DailyUseGateError("sampler-failed")
        previous = None
        for entry in intervals:
            start, end = (
                entry["guest_monotonic_start_ns"],
                entry["guest_monotonic_end_ns"],
            )
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 < start < end
                or (previous is not None and previous != start)
            ):
                raise DailyUseGateError("sampler-coverage-invalid")
            previous = end
        return SamplerCapture(
            payload, intervals[0]["guest_monotonic_start_ns"], previous
        )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise DailyUseGateError("parameters-invalid")


def main(argv: list[str] | None = None) -> int:
    """Run the local gate and emit one bounded terminal verdict."""
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--firefox-pid", type=int, required=True)
    parser.add_argument("--xorg-pid", type=int, required=True)
    parser.add_argument("--fixture-index-url")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("smoke", "profile"))
    parser.add_argument("--physical", action="store_true")
    parser.add_argument("--context-cpu-diagnostic", action="store_true")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--port", type=int, default=2828)
    try:
        options = parser.parse_args(argv)
        mode = options.mode or ("profile" if options.physical else "smoke")
        timeout = options.timeout_seconds
        if timeout is None:
            timeout = float(composite_capture.MODES[mode]["deadline_seconds"])
        if not 1 <= options.port <= 65535 or any(
            char in str(options.evidence_dir) for char in "\r\n"
        ):
            raise DailyUseGateError("parameters-invalid")
        run_id = secrets.token_hex(16)
        options.evidence_dir.mkdir(mode=0o700, exist_ok=True)
        url = _validate_inputs(
            options.firefox_pid,
            options.xorg_pid,
            options.evidence_dir,
            mode,
            timeout,
            run_id,
            options.fixture_index_url,
        )
        operations = default_operations(
            context_cpu_diagnostic=options.context_cpu_diagnostic
        )
        setup_timeout = (
            PHYSICAL_SESSION_SETUP_TIMEOUT_SECONDS if options.physical else timeout
        )
        client = _connect(
            "127.0.0.1",
            options.port,
            operations.clock.monotonic() + setup_timeout,
        )
        result = run_daily_use_gate(
            client=client,
            operations=operations,
            firefox_pid=options.firefox_pid,
            xorg_pid=options.xorg_pid,
            evidence_dir=options.evidence_dir,
            mode=mode,
            timeout_seconds=timeout,
            run_id=run_id,
            fixture_index_url=url,
            physical=options.physical,
        )
        if result["state"] != "pass":
            raise DailyUseGateError("contract-invalid")
    except Exception as error:
        reason = (
            str(error)
            if type(error) is DailyUseGateError and str(error) in _FAILURE_REASONS
            else "phase-failed"
        )
        print(f"ASTERINAS_BROWSER_DAILY_USE_FAIL reason={reason}", file=sys.stderr)
        return 1
    print(
        f"ASTERINAS_BROWSER_DAILY_USE_PASS functions=7/7 slow={result['slowCount']} evidence_dir={options.evidence_dir}"
    )
    return 0


def _validate_inputs(firefox_pid, xorg_pid, evidence_dir, mode, timeout, run_id, url):
    if (
        type(firefox_pid) is not int
        or firefox_pid <= 0
        or type(xorg_pid) is not int
        or xorg_pid <= 0
        or firefox_pid == xorg_pid
        or mode not in ("smoke", "profile")
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        or not isinstance(run_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
    ):
        raise DailyUseGateError("parameters-invalid")
    if (
        not isinstance(evidence_dir, Path)
        or not evidence_dir.is_absolute()
        or evidence_dir.resolve() != evidence_dir
    ):
        raise DailyUseGateError("evidence-directory-invalid")
    try:
        status = evidence_dir.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_mode & 0o077
            or status.st_uid != os.geteuid()
        ):
            raise DailyUseGateError("evidence-directory-invalid")
    except DailyUseGateError:
        raise
    except OSError as error:
        raise DailyUseGateError("evidence-directory-invalid") from error
    try:
        return resolve_fixture_index_url(url)
    except (OSError, ValueError) as error:
        raise DailyUseGateError("parameters-invalid") from error


def _reserve_evidence(evidence_dir: Path, run_id: str) -> Path:
    if any(
        os.path.lexists(evidence_dir / name)
        for name in (*ARTIFACT_NAMES, RESULT_NAME, CHECKPOINT_NAME)
    ):
        raise DailyUseGateError("evidence-exists")
    # Reject any interrupted run, even one with a different run identity.
    if any(
        path.name.startswith(".browser-daily-use-") for path in evidence_dir.iterdir()
    ):
        raise DailyUseGateError("evidence-exists")
    staging = evidence_dir / (".browser-daily-use-" + run_id)
    try:
        # Different run IDs must still compete for one atomic reservation.
        # Keep it after completion so interrupted evidence cannot be reused.
        _write_private(evidence_dir / ".browser-daily-use-reservation", run_id.encode())
        staging.mkdir(mode=0o700)
    except OSError as error:
        raise DailyUseGateError("evidence-exists") from error
    return staging


def _identities(operations, pids):
    ticks = operations.identity_reader(pids)
    if (
        type(ticks) is not tuple
        or len(ticks) != 2
        or any(type(value) is not int or value <= 0 for value in ticks)
    ):
        raise DailyUseGateError("identity-invalid")
    return tuple({"pid": pid, "startTimeTicks": tick} for pid, tick in zip(pids, ticks))


def _value(response):
    return response.get("value", response) if isinstance(response, dict) else response


def _select(client, handle):
    client.command("WebDriver:SwitchToWindow", {"handle": handle, "focus": False})


def _cleanup(client, original, baseline_handles, timeout):
    try:
        if (
            type(baseline_handles) is not tuple
            or not baseline_handles
            or original not in baseline_handles
            or len(set(baseline_handles)) != len(baseline_handles)
        ):
            raise DailyUseGateError("cleanup-failed")
        client.set_timeout(timeout)
        handles = _value(client.command("WebDriver:GetWindowHandles"))
        if (
            type(handles) is not list
            or original not in handles
            or any(type(handle) is not str or not handle for handle in handles)
            or len(set(handles)) != len(handles)
        ):
            raise DailyUseGateError("cleanup-failed")
        for handle in handles:
            if handle not in baseline_handles:
                _select(client, handle)
                client.command("WebDriver:CloseWindow")
        _select(client, original)
        if (
            _value(client.command("WebDriver:GetWindowHandles"))
            != list(baseline_handles)
            or _value(client.command("WebDriver:GetWindowHandle")) != original
        ):
            raise DailyUseGateError("cleanup-failed")
    except Exception as error:
        # Selection remains best effort even if an extra window cannot close.
        try:
            _select(client, original)
        except Exception:
            pass
        raise DailyUseGateError("cleanup-failed") from error


@dataclass
class _Sampler:
    name: str
    operation: Callable[[SamplerRequest], SamplerCapture]
    request: SamplerRequest
    done: threading.Event = field(default_factory=threading.Event)
    result: SamplerCapture | None = None
    failed: bool = False

    def start(self):
        # A broken adapter must not keep the process alive after a stop timeout.
        # It has no publication capability, so a late return is harmless.
        threading.Thread(
            target=self._run, name="daily-use-" + self.name, daemon=True
        ).start()

    def _run(self):
        try:
            self.result = self.operation(self.request)
        except BaseException:
            self.failed = True
        finally:
            if not self.request.stop.is_set():
                self.failed = True
            self.done.set()


def _check_running(samplers):
    if any(sampler.done.is_set() or sampler.failed for sampler in samplers):
        raise DailyUseGateError("sampler-failed")


def _wait_ready(samplers, timeout, clock):
    deadline = clock.monotonic() + timeout
    while True:
        _check_running(samplers)
        if all(sampler.request.ready.is_set() for sampler in samplers):
            return
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise DailyUseGateError("sampler-ready-timeout")
        for sampler in samplers:
            sampler.request.ready.wait(min(0.005, remaining))


def _stop_samplers(samplers, timeout, clock):
    if not samplers:
        return
    for sampler in samplers:
        sampler.request.stop.set()
    deadline = clock.monotonic() + timeout
    for sampler in samplers:
        if not sampler.done.wait(max(0, deadline - clock.monotonic())):
            raise DailyUseGateError("sampler-stop-timeout")
    if any(sampler.failed for sampler in samplers):
        raise DailyUseGateError("sampler-failed")


def _artifact_bytes(payload):
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_ARTIFACT_BYTES:
        raise DailyUseGateError("artifact-invalid")
    return payload


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()


def _write_private(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(source, destination):
    fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise DailyUseGateError("evidence-exists") from error
        try:
            os.fsync(fd)
            source.unlink()
        except OSError:
            # Retract only the link we created, never a racing existing file.
            destination.unlink()
            raise
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
