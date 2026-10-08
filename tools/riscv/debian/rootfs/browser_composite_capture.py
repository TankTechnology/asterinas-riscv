#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture one deterministic phased Firefox workload without a restart."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import threading
import time

if Path("/run/asterinas-tools/browser_perf_capture.py").is_file():
    sys.path.insert(0, "/run/asterinas-tools")
    from browser_perf_capture import (  # type: ignore[import-not-found]
        CaptureError,
        _connect,
        _json_value,
        _navigate,
        _private_json,
        _process_starttimes,
        _script,
        _value,
        _wait_document,
        performance_urls,
        resolve_fixture_index_url,
    )
    from browser_workload_contract import (  # type: ignore[import-not-found]
        MODES,
        WorkloadContractError,
        validate_run_id,
        validate_workload_snapshot,
    )
else:
    from tools.riscv.debian.rootfs.browser_perf_capture import (
        CaptureError,
        _connect,
        _json_value,
        _navigate,
        _private_json,
        _process_starttimes,
        _script,
        _value,
        _wait_document,
        performance_urls,
        resolve_fixture_index_url,
    )
    from tools.riscv.debian.rootfs.browser_workload_contract import (
        MODES,
        WorkloadContractError,
        validate_run_id,
        validate_workload_snapshot,
    )


class CompositeCaptureError(ValueError):
    """The composite workload could not produce bounded evidence."""


SAMPLE_SCHEDULES = {
    "smoke": (0.5, 64),
    "profile": (2.0, 64),
    "stress": (5.0, 64),
}
MAX_CHECKPOINT_BYTES = 256 * 1024
DOCUMENT_SETUP_TIMEOUT_SECONDS = 120.0
SAMPLER_READY_TIMEOUT_SECONDS = 10.0
SAMPLER_STOP_TIMEOUT_SECONDS = 10.0


def workload_url(index_url: str, run_id: str | None = None) -> str:
    """Derive the exact workload page from a validated local fixture URL."""

    try:
        first_url, _ = performance_urls(index_url)
    except CaptureError as error:
        raise CompositeCaptureError("workload fixture URL is invalid") from error
    base = first_url.rsplit("/", 1)[0] + "/workload.html"
    if run_id is None:
        return base
    try:
        validated = validate_run_id(run_id)
    except WorkloadContractError as error:
        raise CompositeCaptureError("workload run identity is invalid") from error
    return f"{base}?run={validated}"


def _completed_phase_names(workload: dict[str, object]) -> list[str]:
    phases = workload["phases"]
    assert isinstance(phases, list)
    return [
        str(phase["name"])
        for phase in phases
        if isinstance(phase, dict) and phase.get("state") == "complete"
    ]


def capture_composite(
    client: object,
    fixture_index_url: str,
    *,
    mode: str,
    run_id: str,
    timeout_seconds: float,
    checkpoint_fn: Callable[[dict[str, object]], object] | None = None,
    before_start_fn: Callable[[], object] | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Run and observe all workload phases in one existing Firefox session."""

    if mode not in MODES:
        raise CompositeCaptureError("composite workload mode is invalid")
    deadline_bound = MODES[mode]["deadline_seconds"]
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 1 <= timeout_seconds <= deadline_bound
    ):
        raise CompositeCaptureError("composite workload timeout is invalid")
    run_id = validate_run_id(run_id)
    url = workload_url(fixture_index_url, run_id)
    _navigate(client, url)
    _wait_document(client, url, time.monotonic() + DOCUMENT_SETUP_TIMEOUT_SECONDS)
    if before_start_fn is not None:
        before_start_fn()
    workload_start_observed_ns = clock_ns()
    started = _script(
        client,
        "if (document.URL !== arguments[0] || "
        "!window.wrappedJSObject.__asterinasStartCompositeWorkload) "
        "return 'missing'; "
        "window.wrappedJSObject.__asterinasStartCompositeWorkload(arguments[1]); "
        "return 'started';",
        [url, mode],
    )
    if started != "started":
        raise CompositeCaptureError("composite workload did not start")
    deadline = time.monotonic() + timeout_seconds

    snapshot_script = (
        "return JSON.stringify({url: document.URL, workload: "
        "window.wrappedJSObject.__asterinasCompositeWorkloadSnapshot ? "
        "window.wrappedJSObject.__asterinasCompositeWorkloadSnapshot() : null});"
    )
    observations: list[dict[str, object]] = []
    observed_count = 0
    completed_prefix: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        try:
            envelope = _json_value(_script(client, snapshot_script))
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"url", "workload"}
                or envelope["url"] != url
            ):
                raise CompositeCaptureError("composite workload document changed")
            workload = validate_workload_snapshot(
                envelope["workload"],
                expected_mode=mode,
                expected_run_id=run_id,
                allow_running=True,
            )
        except CompositeCaptureError:
            raise
        except (CaptureError, WorkloadContractError, TimeoutError) as error:
            raise CompositeCaptureError(
                "composite workload snapshot is invalid"
            ) from error
        completed = _completed_phase_names(workload)
        if len(completed) < observed_count:
            raise CompositeCaptureError("completed workload phase count regressed")
        phases = workload["phases"]
        if not isinstance(phases, list):
            raise CompositeCaptureError("composite workload phases are unavailable")
        if phases[:observed_count] != completed_prefix:
            raise CompositeCaptureError("completed phase changed after publication")
        if len(completed) > observed_count:
            for name in completed[observed_count:]:
                observations.append(
                    {"phase": name, "observed_guest_monotonic_ns": clock_ns()}
                )
            observed_count = len(completed)
            completed_prefix = copy.deepcopy(phases[:observed_count])
            if checkpoint_fn is not None:
                checkpoint_fn(
                    {
                        "schema_version": 1,
                        "clock_domain": "guest-monotonic-observation",
                        "mode": mode,
                        "run_id": run_id,
                        "completed_phases": completed,
                        "phase_observations": list(observations),
                        "workload": workload,
                    }
                )
        if workload["state"] == "failed":
            raise CompositeCaptureError(
                f"composite workload reported failure: {workload['error']}"
            )
        if workload["state"] == "complete":
            try:
                terminal = validate_workload_snapshot(
                    workload,
                    expected_mode=mode,
                    expected_run_id=run_id,
                    allow_running=False,
                )
            except WorkloadContractError as error:
                raise CompositeCaptureError(
                    "composite workload terminal snapshot is invalid"
                ) from error
            return {
                "schema_version": 1,
                "clock_domain": "browser-and-guest-monotonic-separated",
                "workload_url": url,
                "run_id": run_id,
                "workload_start_observed_guest_monotonic_ns": (
                    workload_start_observed_ns
                ),
                "workload": terminal,
                "phase_observations": observations,
            }
        sleep_fn(0.05 if mode == "smoke" else 0.1)
    raise CompositeCaptureError("composite workload deadline expired")


def _atomic_private_json(path: Path, report: dict[str, object]) -> None:
    payload = (
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise CompositeCaptureError("composite checkpoint is oversized")
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
        raise CompositeCaptureError("composite checkpoint is not exclusive") from error
    try:
        cursor = 0
        while cursor < len(payload):
            count = os.write(descriptor, payload[cursor:])
            if count <= 0:
                raise CompositeCaptureError("composite checkpoint did not advance")
            cursor += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _private_marker(path: Path, marker_ns: int) -> None:
    payload = f"{marker_ns}\n".encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise CompositeCaptureError("workload ready marker is not exclusive") from error
    try:
        if os.write(descriptor, payload) != len(payload):
            raise CompositeCaptureError("workload ready marker is incomplete")
    finally:
        os.close(descriptor)


def _sample_system(
    path: Path,
    pids: tuple[int, int],
    mode: str,
    ready: threading.Event,
    stop: threading.Event,
) -> None:
    if Path("/run/asterinas-tools/browser_system_time.py").is_file():
        sys.path.insert(0, "/run/asterinas-tools")
        from browser_system_time import run_sampler  # type: ignore[import-not-found]
    else:
        from tools.riscv.debian.rootfs.browser_system_time import run_sampler

    interval_seconds, samples = SAMPLE_SCHEDULES[mode]
    run_sampler(
        Path("/proc"),
        pids,
        path,
        interval_seconds=interval_seconds,
        samples=samples,
        ready_fn=ready.set,
        stop_event=stop,
    )


def _sample_threads(
    path: Path,
    pid: int,
    marker: Path,
    mode: str,
    physical: bool,
    ready: threading.Event,
    stop: threading.Event,
) -> None:
    if Path("/run/asterinas-tools/browser_system_time.py").is_file():
        sys.path.insert(0, "/run/asterinas-tools")
        from browser_system_time import (  # type: ignore[import-not-found]
            run_thread_sampler,
        )
    else:
        from tools.riscv.debian.rootfs.browser_system_time import run_thread_sampler

    interval_seconds, samples = SAMPLE_SCHEDULES[mode]
    run_thread_sampler(
        Path("/proc"),
        pid,
        marker,
        path,
        interval_seconds=interval_seconds,
        samples=samples,
        physical=physical,
        ready_fn=ready.set,
        stop_event=stop,
    )


def _evidence_bounds(path: Path) -> tuple[int, int]:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise CompositeCaptureError("composite system evidence is oversized")
        report = json.loads(path.read_text())
        intervals = report["intervals"]
        first = intervals[0]["guest_monotonic_start_ns"]
        last = intervals[-1]["guest_monotonic_end_ns"]
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise CompositeCaptureError("composite system evidence is malformed") from error
    if type(first) is not int or type(last) is not int or first < 0 or last <= first:
        raise CompositeCaptureError("composite system evidence clock is invalid")
    return first, last


def run_composite_capture(
    client: object,
    fixture_index_url: str,
    *,
    firefox_pid: int,
    xorg_pid: int,
    evidence_dir: Path,
    mode: str,
    timeout_seconds: float,
    physical: bool = False,
    system_sample_fn: Callable[
        [Path, tuple[int, int], str, threading.Event, threading.Event], object
    ] = _sample_system,
    thread_sample_fn: Callable[
        [Path, int, Path, str, bool, threading.Event, threading.Event], object
    ] = _sample_threads,
    identity_fn: Callable[[tuple[int, int]], tuple[int, int]] = _process_starttimes,
    run_id_fn: Callable[[], str] = lambda: secrets.token_hex(16),
    sampler_stop_timeout_seconds: float = SAMPLER_STOP_TIMEOUT_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Publish composite browser, process, and thread evidence together."""

    if (
        type(firefox_pid) is not int
        or firefox_pid <= 0
        or type(xorg_pid) is not int
        or xorg_pid <= 0
        or firefox_pid == xorg_pid
        or type(physical) is not bool
        or mode not in MODES
        or not evidence_dir.is_absolute()
        or isinstance(sampler_stop_timeout_seconds, bool)
        or not isinstance(sampler_stop_timeout_seconds, (int, float))
        or not 0 < sampler_stop_timeout_seconds <= 30
    ):
        raise CompositeCaptureError("composite capture inputs are invalid")
    workload_url(fixture_index_url)
    try:
        run_id = validate_run_id(run_id_fn())
    except (WorkloadContractError, TypeError) as error:
        raise CompositeCaptureError(
            "composite workload run identity is invalid"
        ) from error
    if (
        not evidence_dir.is_dir()
        or evidence_dir.is_symlink()
        or not stat.S_ISDIR(evidence_dir.stat().st_mode)
    ):
        raise CompositeCaptureError("composite evidence directory is unavailable")
    paths = {
        "capture": evidence_dir / "browser-composite-capture.json",
        "checkpoint": evidence_dir / "browser-composite-checkpoint.json",
        "system": evidence_dir / "browser-system-time.json",
        "thread": evidence_dir / "browser-thread-time.json",
    }
    if any(os.path.lexists(path) for path in paths.values()):
        raise CompositeCaptureError("composite evidence artifact already exists")
    sampler_directory = evidence_dir / f".browser-composite-samplers.{run_id}"
    if os.path.lexists(sampler_directory):
        raise CompositeCaptureError("composite sampler directory already exists")
    sampler_paths = {
        "system": sampler_directory / paths["system"].name,
        "thread": sampler_directory / paths["thread"].name,
        "ready": sampler_directory / "browser-composite-ready",
    }

    process_ids = (firefox_pid, xorg_pid)
    initial_starttimes = identity_fn(process_ids)
    client.set_timeout(timeout_seconds)  # type: ignore[attr-defined]
    session = _value(
        client.command(  # type: ignore[attr-defined]
            "WebDriver:NewSession",
            {
                "acceptInsecureCerts": False,
                "pageLoadStrategy": "none",
                "strictFileInteractability": True,
            },
        )
    )
    if (
        not isinstance(session, dict)
        or not isinstance(session.get("sessionId"), str)
        or not isinstance(session.get("capabilities"), dict)
        or session["capabilities"].get("acceptInsecureCerts") is not False
    ):
        raise CompositeCaptureError("Firefox did not create a verified session")
    handles = _value(client.command("WebDriver:GetWindowHandles"))  # type: ignore[attr-defined]
    if (
        not isinstance(handles, list)
        or not handles
        or len(handles) > 16
        or not all(isinstance(handle, str) for handle in handles)
    ):
        raise CompositeCaptureError("Firefox composite window is unavailable")
    switched = _value(
        client.command(  # type: ignore[attr-defined]
            "WebDriver:SwitchToWindow", {"handle": handles[0], "focus": False}
        )
    )
    if switched is not None:
        raise CompositeCaptureError("Firefox composite window selection failed")
    client.set_timeout(timeout_seconds)  # type: ignore[attr-defined]

    sample_error: BaseException | None = None
    sample_error_lock = threading.Lock()
    sampler_ready = (threading.Event(), threading.Event())
    sampler_stop = threading.Event()
    evidence_started = False
    started_samplers: list[threading.Thread] = []

    def record_sample_error(error: BaseException) -> None:
        nonlocal sample_error
        with sample_error_lock:
            if sample_error is None:
                sample_error = error

    def current_sample_error() -> BaseException | None:
        with sample_error_lock:
            return sample_error

    def sample_system() -> None:
        try:
            system_sample_fn(
                sampler_paths["system"],
                process_ids,
                mode,
                sampler_ready[0],
                sampler_stop,
            )
        except BaseException as error:
            record_sample_error(error)

    def sample_threads() -> None:
        try:
            thread_sample_fn(
                sampler_paths["thread"],
                firefox_pid,
                sampler_paths["ready"],
                mode,
                physical,
                sampler_ready[1],
                sampler_stop,
            )
        except BaseException as error:
            record_sample_error(error)

    samplers = (
        threading.Thread(
            target=sample_system, name="browser-system-sampler", daemon=True
        ),
        threading.Thread(
            target=sample_threads, name="browser-thread-sampler", daemon=True
        ),
    )

    def start_evidence() -> None:
        nonlocal evidence_started
        if evidence_started:
            raise CompositeCaptureError("composite samplers started more than once")
        try:
            sampler_directory.mkdir(mode=0o700)
        except OSError as error:
            raise CompositeCaptureError(
                "composite sampler directory is unavailable"
            ) from error
        _private_marker(sampler_paths["ready"], time.monotonic_ns())
        evidence_started = True
        for sampler in samplers:
            sampler.start()
            started_samplers.append(sampler)
        ready_deadline = time.monotonic() + SAMPLER_READY_TIMEOUT_SECONDS
        while not all(event.is_set() for event in sampler_ready):
            error = current_sample_error()
            if error is not None:
                raise CompositeCaptureError(
                    "composite sampler failed before workload"
                ) from error
            remaining = ready_deadline - time.monotonic()
            if remaining <= 0:
                raise CompositeCaptureError("composite sampler readiness expired")
            for event in sampler_ready:
                event.wait(min(0.01, remaining))

    try:
        try:
            report = capture_composite(
                client,
                fixture_index_url,
                mode=mode,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                checkpoint_fn=lambda checkpoint: _atomic_private_json(
                    paths["checkpoint"], checkpoint
                ),
                before_start_fn=start_evidence,
                sleep_fn=sleep_fn,
            )
        finally:
            sampler_stop.set()
            stop_deadline = time.monotonic() + sampler_stop_timeout_seconds
            for sampler in started_samplers:
                sampler.join(max(0.0, stop_deadline - time.monotonic()))
            if any(sampler.is_alive() for sampler in started_samplers):
                raise CompositeCaptureError("composite sampler stop expired")
    except BaseException:
        shutil.rmtree(sampler_directory, ignore_errors=True)
        raise

    def discard_sampler_evidence() -> None:
        shutil.rmtree(sampler_directory, ignore_errors=True)
        for name in ("system", "thread"):
            try:
                paths[name].unlink(missing_ok=True)
            except OSError:
                pass

    sampling_failure = current_sample_error()
    if (
        sampling_failure is not None
        or not sampler_paths["system"].is_file()
        or not sampler_paths["thread"].is_file()
    ):
        discard_sampler_evidence()
        raise CompositeCaptureError("composite system evidence is unavailable") from (
            sampling_failure
        )
    for path in (sampler_paths["system"], sampler_paths["thread"]):
        os.chmod(path, 0o600, follow_symlinks=False)
    start_ns = report["workload_start_observed_guest_monotonic_ns"]
    observations = report["phase_observations"]
    if (
        type(start_ns) is not int
        or not isinstance(observations, list)
        or not observations
    ):
        discard_sampler_evidence()
        raise CompositeCaptureError("composite workload observation clock is invalid")
    end_ns = observations[-1]["observed_guest_monotonic_ns"]
    if type(end_ns) is not int or end_ns < start_ns:
        discard_sampler_evidence()
        raise CompositeCaptureError("composite workload observation clock is invalid")
    for role in ("system", "thread"):
        path = sampler_paths[role]
        evidence_start, evidence_end = _evidence_bounds(path)
        if evidence_start > start_ns or evidence_end < end_ns:
            discard_sampler_evidence()
            raise CompositeCaptureError(
                f"composite {role} evidence does not cover workload "
                f"evidence=[{evidence_start},{evidence_end}] "
                f"workload=[{start_ns},{end_ns}]"
            )
    if identity_fn(process_ids) != initial_starttimes:
        discard_sampler_evidence()
        raise CompositeCaptureError("Firefox/Xorg identity changed during workload")

    try:
        for name in ("system", "thread"):
            os.replace(sampler_paths[name], paths[name])
    except OSError as error:
        discard_sampler_evidence()
        raise CompositeCaptureError("composite evidence publication failed") from error
    shutil.rmtree(sampler_directory)

    report.update(
        {
            "firefox_pid": firefox_pid,
            "xorg_pid": xorg_pid,
            "process_starttime_ticks": list(initial_starttimes),
            "mode": mode,
            "physical": physical,
            "system_artifact": paths["system"].name,
            "thread_artifact": paths["thread"].name,
            "checkpoint_artifact": paths["checkpoint"].name,
        }
    )
    _private_json(paths["capture"], report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firefox-pid", type=int, required=True)
    parser.add_argument("--xorg-pid", type=int, required=True)
    parser.add_argument("--mode", choices=tuple(MODES), default="profile")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--physical", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--fixture-index-url")
    parser.add_argument("--port", type=int, default=2828)
    options = parser.parse_args()
    timeout_seconds = (
        float(MODES[options.mode]["deadline_seconds"])
        if options.timeout_seconds is None
        else options.timeout_seconds
    )
    if not 1 <= options.port <= 65535:
        parser.error("Marionette port is invalid")
    try:
        options.evidence_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        index_url = resolve_fixture_index_url(options.fixture_index_url)
        client = _connect("127.0.0.1", options.port, time.monotonic() + timeout_seconds)
        try:
            report = run_composite_capture(
                client,
                index_url,
                firefox_pid=options.firefox_pid,
                xorg_pid=options.xorg_pid,
                evidence_dir=options.evidence_dir,
                mode=options.mode,
                timeout_seconds=timeout_seconds,
                physical=options.physical,
            )
        finally:
            client.close()
    except (CompositeCaptureError, CaptureError, OSError, TimeoutError) as error:
        detail = json.dumps(str(error)[:120])
        print(
            "ASTERINAS_BROWSER_COMPOSITE_FAIL "
            f"reason={type(error).__name__} detail={detail}",
            file=sys.stderr,
        )
        return 1
    print(
        "ASTERINAS_BROWSER_COMPOSITE_PASS "
        f"mode={options.mode} phases={len(report['phase_observations'])} "
        f"evidence_dir={options.evidence_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
