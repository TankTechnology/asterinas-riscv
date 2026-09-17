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

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from typing import Protocol

if Path("/run/asterinas-tools/browser_daily_use_contract.py").is_file():
    sys.path.insert(0, "/run/asterinas-tools")
    from browser_daily_use_contract import (  # type: ignore[import-not-found]
        MAX_ARTIFACT_BYTES,
        DailyUseContractError,
        _normalize_function_groups,
        build_daily_use_result,
    )
    from browser_perf_capture import (  # type: ignore[import-not-found]
        _process_starttimes,
        resolve_fixture_index_url,
    )
else:
    from tools.riscv.debian.rootfs.browser_daily_use_contract import (
        MAX_ARTIFACT_BYTES,
        DailyUseContractError,
        _normalize_function_groups,
        build_daily_use_result,
    )
    from tools.riscv.debian.rootfs.browser_perf_capture import (
        _process_starttimes,
        resolve_fixture_index_url,
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
    def close(self) -> None: ...


class ExistingSession:
    """Expose browser commands without session creation, deletion, or closure."""

    def __init__(self, client: BrowserTransport):
        self.__client = client

    def command(self, name: str, parameters: dict | None = None) -> object:
        if name in {
            "WebDriver:NewSession",
            "WebDriver:DeleteSession",
            "Marionette:Quit",
        }:
            raise DailyUseGateError("session-command-forbidden")
        return self.__client.command(name, parameters)


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


@dataclass(frozen=True)
class FixtureCapture:
    function_groups: list[dict[str, object]]
    artifact: bytes


@dataclass(frozen=True)
class TimingCapture:
    performance: list[dict[str, object]]
    artifact: bytes


@dataclass(frozen=True)
class ContextCapture:
    performance: dict[str, object]
    artifact: bytes


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
    samplers: list[_Sampler] = []
    cleaned = False
    transport_closed = False
    clock = operations.clock
    try:
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
        client.set_timeout(timeout_seconds)
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
        # Remember the window even when capabilities are invalid, so cleanup
        # still restores the desktop if session setup partially succeeded.
        original = _value(client.command("WebDriver:GetWindowHandle"))
        if not isinstance(original, str) or not original:
            original = None
            raise DailyUseGateError("session-invalid")
        _select(client, original)
        if (
            not isinstance(session, dict)
            or not isinstance(session.get("sessionId"), str)
            or not session["sessionId"]
            or not isinstance(session.get("capabilities"), dict)
            or session["capabilities"].get("acceptInsecureCerts") is not False
        ):
            raise DailyUseGateError("session-invalid")
        completed.append("session")

        # The upper bound includes four browser phases and both handshakes.
        sampler_deadline = clock.monotonic() + 6 * timeout_seconds
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
            request = CaptureRequest(
                ExistingSession(client),
                original,
                fixture_url,
                mode,
                firefox_pid,
                xorg_pid,
                clock.monotonic() + timeout_seconds,
                clock,
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
                function_groups = _normalize_function_groups(capture.function_groups)
            captures.append(capture)
            completed.append(phase)
        workload_end_ns = clock.monotonic_ns()
        _stop_samplers(samplers, timeout_seconds, clock)
        completed.append("samplers-stopped")
        _cleanup(client, original, timeout_seconds)
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
            limitations=composite.limitations,
        )
        completed.append("validated")
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
        if original is not None and not cleaned:
            try:
                _cleanup(client, original, timeout_seconds)
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
                    "functionGroups": _normalize_function_groups(function_groups)
                    if "fixture" in completed
                    else [],
                    "failure": {"type": "daily-use-gate", "reason": reason},
                }
                _write_private(staging / CHECKPOINT_NAME, _json_bytes(checkpoint))
                _publish(staging / CHECKPOINT_NAME, evidence_dir / CHECKPOINT_NAME)
            except (OSError, DailyUseGateError, DailyUseContractError):
                # No checkpoint is promised if evidence persistence is broken.
                pass
        raise DailyUseGateError(reason) from error


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


def _cleanup(client, original, timeout):
    try:
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
            if handle != original:
                _select(client, handle)
                client.command("WebDriver:CloseWindow")
        _select(client, original)
        if (
            _value(client.command("WebDriver:GetWindowHandles")) != [original]
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
