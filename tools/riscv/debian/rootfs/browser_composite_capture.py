#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture one deterministic phased Firefox workload without a restart."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
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
        validate_workload_snapshot,
    )


class CompositeCaptureError(ValueError):
    """The composite workload could not produce bounded evidence."""


SAMPLE_SCHEDULES = {
    "smoke": (0.5, 30),
    "profile": (1.0, 64),
    "stress": (2.5, 64),
}
MAX_CHECKPOINT_BYTES = 256 * 1024
DOCUMENT_SETUP_TIMEOUT_SECONDS = 120.0


def workload_url(index_url: str) -> str:
    """Derive the exact workload page from a validated local fixture URL."""

    try:
        first_url, _ = performance_urls(index_url)
    except CaptureError as error:
        raise CompositeCaptureError("workload fixture URL is invalid") from error
    return first_url.rsplit("/", 1)[0] + "/workload.html"


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
    timeout_seconds: float,
    checkpoint_fn: Callable[[dict[str, object]], object] | None = None,
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
    url = workload_url(fixture_index_url)
    _navigate(client, url)
    _wait_document(client, url, time.monotonic() + DOCUMENT_SETUP_TIMEOUT_SECONDS)
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
                envelope["workload"], expected_mode=mode, allow_running=True
            )
        except CompositeCaptureError:
            raise
        except (CaptureError, WorkloadContractError, TimeoutError) as error:
            raise CompositeCaptureError("composite workload snapshot is invalid") from error
        completed = _completed_phase_names(workload)
        if len(completed) < observed_count:
            raise CompositeCaptureError("completed workload phase count regressed")
        if len(completed) > observed_count:
            for name in completed[observed_count:]:
                observations.append(
                    {"phase": name, "observed_guest_monotonic_ns": clock_ns()}
                )
            observed_count = len(completed)
            if checkpoint_fn is not None:
                checkpoint_fn(
                    {
                        "schema_version": 1,
                        "clock_domain": "guest-monotonic-observation",
                        "mode": mode,
                        "completed_phases": completed,
                        "phase_observations": list(observations),
                        "workload": workload,
                    }
                )
        if workload["state"] == "failed":
            raise CompositeCaptureError("composite workload reported failure")
        if workload["state"] == "complete":
            try:
                terminal = validate_workload_snapshot(
                    workload, expected_mode=mode, allow_running=False
                )
            except WorkloadContractError as error:
                raise CompositeCaptureError(
                    "composite workload terminal snapshot is invalid"
                ) from error
            return {
                "schema_version": 1,
                "clock_domain": "browser-and-guest-monotonic-separated",
                "workload_url": url,
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


def _sample_system(path: Path, pids: tuple[int, int], mode: str) -> None:
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
    )


def _sample_threads(
    path: Path,
    pid: int,
    marker: Path,
    mode: str,
    physical: bool,
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
    )


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
    system_sample_fn: Callable[[Path, tuple[int, int], str], object] = _sample_system,
    thread_sample_fn: Callable[[Path, int, Path, str, bool], object] = _sample_threads,
    identity_fn: Callable[[tuple[int, int]], tuple[int, int]] = _process_starttimes,
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
    ):
        raise CompositeCaptureError("composite capture inputs are invalid")
    workload_url(fixture_index_url)
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
        "ready": evidence_dir / "browser-composite-ready",
    }
    if any(os.path.lexists(path) for path in paths.values()):
        raise CompositeCaptureError("composite evidence artifact already exists")

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

    _private_marker(paths["ready"], time.monotonic_ns())
    sample_errors: list[BaseException] = []

    def sample_system() -> None:
        try:
            system_sample_fn(paths["system"], process_ids, mode)
        except BaseException as error:
            sample_errors.append(error)

    def sample_threads() -> None:
        try:
            thread_sample_fn(
                paths["thread"], firefox_pid, paths["ready"], mode, physical
            )
        except BaseException as error:
            sample_errors.append(error)

    samplers = (
        threading.Thread(target=sample_system, name="browser-system-sampler"),
        threading.Thread(target=sample_threads, name="browser-thread-sampler"),
    )
    for sampler in samplers:
        sampler.start()
    try:
        report = capture_composite(
            client,
            fixture_index_url,
            mode=mode,
            timeout_seconds=timeout_seconds,
            checkpoint_fn=lambda checkpoint: _atomic_private_json(
                paths["checkpoint"], checkpoint
            ),
            sleep_fn=sleep_fn,
        )
    finally:
        for sampler in samplers:
            sampler.join()
    if sample_errors or not paths["system"].is_file() or not paths["thread"].is_file():
        raise CompositeCaptureError("composite system evidence is unavailable") from (
            sample_errors[0] if sample_errors else None
        )
    for path in (paths["system"], paths["thread"]):
        os.chmod(path, 0o600, follow_symlinks=False)
    if identity_fn(process_ids) != initial_starttimes:
        raise CompositeCaptureError("Firefox/Xorg identity changed during workload")

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
        client = _connect(
            "127.0.0.1", options.port, time.monotonic() + timeout_seconds
        )
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
