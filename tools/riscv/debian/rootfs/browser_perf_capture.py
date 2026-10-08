#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture one local Firefox timing workload without closing the desktop."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
import time
from urllib.parse import urlparse

if Path("/run/asterinas-tools/browser_latency_contract.py").is_file():
    sys.path.insert(0, "/run/asterinas-tools")
    from browser_latency_contract import (  # type: ignore[import-not-found]
        BrowserLatencyError,
        NAVIGATION_FIELDS,
        summarize_interactions,
        validate_navigation,
    )
else:
    from tools.riscv.debian.rootfs.browser_latency_contract import (
        BrowserLatencyError,
        NAVIGATION_FIELDS,
        summarize_interactions,
        validate_navigation,
    )

if Path("/run/asterinas-tools/browser_m5_marionette_gate.py").is_file():
    sys.path.insert(0, "/run/asterinas-tools")
    from browser_m5_marionette_gate import _connect  # type: ignore[import-not-found]
else:
    from tools.riscv.debian.rootfs.browser_m5_marionette_gate import _connect


class CaptureError(ValueError):
    """Firefox timing capture could not produce bounded evidence."""


FIXTURE_HOSTS = frozenset({"10.0.2.2", "10.100.19.216"})


def performance_urls(index_url: str) -> tuple[str, str]:
    """Derive two exact local fixture pages from the frozen slirp URL."""

    try:
        parsed = urlparse(index_url)
        port = parsed.port
    except ValueError as error:
        raise CaptureError("performance fixture URL is malformed") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in FIXTURE_HOSTS
        or port != 17894
        or parsed.path != "/browser-quality/index.html"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CaptureError("performance fixture URL is outside the local contract")
    root = f"http://{parsed.hostname}:17894/browser-quality/"
    return root + "perf.html", root + "perf-second.html"


def _value(response: object) -> object:
    if isinstance(response, dict) and "value" in response:
        return response["value"]
    return response


def _script(client: object, script: str, args: list[object] | None = None) -> object:
    return _value(
        client.command(  # type: ignore[attr-defined]
            "WebDriver:ExecuteScript",
            {
                "script": script,
                "args": args or [],
                "newSandbox": True,
                "sandbox": "default",
                "line": 1,
                "filename": "asterinas-browser-perf-capture",
            },
        )
    )


def _json_value(value: object) -> object:
    if not isinstance(value, str) or len(value) > 32 * 1024:
        raise CaptureError("Marionette timing value is absent or oversized")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise CaptureError("Marionette timing value is not JSON") from error


def _navigate(client: object, url: str) -> None:
    result = _value(client.command("WebDriver:Navigate", {"url": url}))  # type: ignore[attr-defined]
    if result is not None:
        raise CaptureError("Marionette navigation did not return null")


def _wait_document(client: object, url: str, deadline: float) -> None:
    script = (
        "return JSON.stringify({url: document.URL, readyState: document.readyState});"
    )
    observed = "unseen"
    while time.monotonic() < deadline:
        try:
            raw = _script(client, script)
        except TimeoutError as error:
            raise CaptureError(
                f"local performance document command timed out observed={observed}"
            ) from error
        if raw is None or raw == "":
            # Firefox may briefly return no script value while replacing the
            # browsing context after Navigate; only readiness is retried.
            time.sleep(0.25)
            continue
        value = _json_value(raw)
        if isinstance(value, dict):
            candidate_url = value.get("url")
            candidate_state = value.get("readyState")
            if candidate_url == url:
                page_kind = "expected"
            elif isinstance(candidate_url, str):
                try:
                    scheme = urlparse(candidate_url).scheme
                except ValueError:
                    scheme = ""
                page_kind = (
                    scheme if scheme in {"about", "http", "https", "file"} else "other"
                )
            else:
                page_kind = "other"
            state = (
                candidate_state
                if isinstance(candidate_state, str)
                and candidate_state in {"loading", "interactive", "complete"}
                else "unknown"
            )
            observed = f"{page_kind}:{state}"
        if (
            isinstance(value, dict)
            and value.get("url") == url
            and value.get("readyState") == "complete"
        ):
            return
        time.sleep(0.25)
    raise CaptureError(
        f"local performance document did not become complete observed={observed}"
    )


def capture_local(
    client: object,
    fixture_index_url: str,
    *,
    synthetic_samples: int,
    timeout_seconds: float,
    interaction_checkpoint_fn: Callable[[dict[str, object]], object] | None = None,
    navigation_validator: Callable[
        [object], dict[str, float | str]
    ] = validate_navigation,
) -> dict[str, object]:
    """Capture isolated browser rAF and local navigation within one session.

    An explicit navigation validator may preserve independently usable timing
    intervals. The default retains the complete, ordered waterfall contract.
    """

    if (
        type(synthetic_samples) is not int
        or not 1 <= synthetic_samples <= 16
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 1 <= timeout_seconds <= 120
    ):
        raise CaptureError("performance capture bounds are invalid")
    first_url, second_url = performance_urls(fixture_index_url)
    deadline = time.monotonic() + timeout_seconds
    command_start_ns = time.monotonic_ns()
    _navigate(client, first_url)
    command_done_ns = time.monotonic_ns()
    _wait_document(client, first_url, deadline)

    started = _script(
        client,
        "if (document.URL !== arguments[0] || "
        "!window.wrappedJSObject.__asterinasRunSyntheticTiming) "
        "return 'missing'; "
        "window.wrappedJSObject.__asterinasRunSyntheticTiming(arguments[1]); "
        "return 'started';",
        [first_url, synthetic_samples],
    )
    if started != "started":
        raise CaptureError("local synthetic timing did not start")
    sample_script = (
        "return JSON.stringify(window.wrappedJSObject.__asterinasTimingSnapshot ? "
        "window.wrappedJSObject.__asterinasTimingSnapshot() : null);"
    )
    interaction_snapshot: object | None = None
    interaction_summary: dict[str, object] | None = None
    while time.monotonic() < deadline:
        interaction_snapshot = _json_value(_script(client, sample_script))
        try:
            interaction_summary = summarize_interactions(
                interaction_snapshot, source="synthetic"
            )
        except BrowserLatencyError:
            time.sleep(0.05)
            continue
        if all(
            interaction_summary[kind]["next_raf_ms"]["count"] >= synthetic_samples  # type: ignore[index]
            for kind in ("keyboard", "pointer", "scroll")
        ):
            break
        time.sleep(0.05)
    else:
        raise CaptureError("local synthetic timing samples did not complete")

    interaction_checkpoint = {
        "schema_version": 1,
        "clock_domain": "browser-request-animation-frame",
        "interaction_snapshot": interaction_snapshot,
        "interaction_summary": interaction_summary,
    }
    if interaction_checkpoint_fn is not None:
        interaction_checkpoint_fn(interaction_checkpoint)

    if os.environ.get("ASTERINAS_BROWSER_PERF_DIAGNOSTICS") == "1":
        print(
            "A_BROWSER_PERF_INTERACTION_DIAGNOSTIC "
            + json.dumps(interaction_summary, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )

    second_start_ns = time.monotonic_ns()
    _navigate(client, second_url)
    second_done_ns = time.monotonic_ns()
    _wait_document(client, second_url, deadline)
    nav_script = (
        "return JSON.stringify(window.wrappedJSObject.__asterinasNavigationSnapshot ? "
        "window.wrappedJSObject.__asterinasNavigationSnapshot() : null);"
    )
    navigation_snapshot: object | None = None
    navigation_parts: dict[str, float | str] | None = None
    last_validation = "unseen"
    diagnostic_reported = False
    while time.monotonic() < deadline:
        try:
            navigation_snapshot = _json_value(_script(client, nav_script))
        except TimeoutError as error:
            raise CaptureError(
                "local navigation timing command timed out "
                f"last_validation={last_validation}"
            ) from error
        try:
            navigation_parts = navigation_validator(navigation_snapshot)
        except BrowserLatencyError as error:
            last_validation = str(error)
            if (
                not diagnostic_reported
                and os.environ.get("ASTERINAS_BROWSER_PERF_DIAGNOSTICS") == "1"
            ):
                fields: dict[str, int | float | str] = {}
                if isinstance(navigation_snapshot, dict):
                    for field in NAVIGATION_FIELDS:
                        number = navigation_snapshot.get(field)
                        try:
                            finite = (
                                not isinstance(number, bool)
                                and isinstance(number, (int, float))
                                and math.isfinite(float(number))
                                and abs(float(number)) <= 1e15
                            )
                        except OverflowError:
                            finite = False
                        fields[field] = number if finite else "invalid"
                record = {"version": 1, "validation": last_validation, "fields": fields}
                print(
                    "A_BROWSER_PERF_NAVIGATION_DIAGNOSTIC "
                    + json.dumps(record, sort_keys=True, separators=(",", ":")),
                    file=sys.stderr,
                    flush=True,
                )
                diagnostic_reported = True
            time.sleep(0.25)
            continue
        break
    else:
        raise CaptureError(
            f"local navigation timing did not complete last_validation={last_validation}"
        )
    return {
        "schema_version": 1,
        "clock_domain": "guest-monotonic-command-and-browser-local-separated",
        "first_url": first_url,
        "second_url": second_url,
        "first_navigation_command_ns": [command_start_ns, command_done_ns],
        "second_navigation_command_ns": [second_start_ns, second_done_ns],
        "interaction_snapshot": interaction_snapshot,
        "interaction_summary": interaction_summary,
        "navigation_snapshot": navigation_snapshot,
        "navigation_parts": navigation_parts,
    }


MAX_CAPTURE_BYTES = 256 * 1024


def _private_json(path: Path, report: dict[str, object]) -> None:
    payload = (
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(payload) > MAX_CAPTURE_BYTES:
        raise CaptureError("browser timing report is oversized")
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
        raise CaptureError("browser timing output is not exclusive") from error
    try:
        cursor = 0
        while cursor < len(payload):
            count = os.write(descriptor, payload[cursor:])
            if count <= 0:
                raise CaptureError("browser timing output did not advance")
            cursor += count
    finally:
        os.close(descriptor)


def _sample_cpu(path: Path, pids: tuple[int, int]) -> None:
    if Path("/run/asterinas-tools/browser_system_time.py").is_file():
        sys.path.insert(0, "/run/asterinas-tools")
        from browser_system_time import run_sampler  # type: ignore[import-not-found]
    else:
        from tools.riscv.debian.rootfs.browser_system_time import run_sampler

    run_sampler(Path("/proc"), pids, path, interval_seconds=0.5, samples=20)


def _process_starttimes(pids: tuple[int, int]) -> tuple[int, int]:
    if Path("/run/asterinas-tools/browser_system_time.py").is_file():
        sys.path.insert(0, "/run/asterinas-tools")
        from browser_system_time import (  # type: ignore[import-not-found]
            parse_pid_stat,
            read_proc_text,
        )
    else:
        from tools.riscv.debian.rootfs.browser_system_time import (
            parse_pid_stat,
            read_proc_text,
        )
    try:
        processes = tuple(
            parse_pid_stat(read_proc_text(Path(f"/proc/{pid}/stat"))) for pid in pids
        )
    except ValueError as error:
        raise CaptureError("Firefox/Xorg process identity is unavailable") from error
    if any(process.pid != pid for process, pid in zip(processes, pids)):
        raise CaptureError("Firefox/Xorg process identity changed")
    return tuple(process.starttime_ticks for process in processes)  # type: ignore[return-value]


def run_capture(
    client: object,
    fixture_index_url: str,
    *,
    firefox_pid: int,
    xorg_pid: int,
    evidence_dir: Path,
    synthetic_samples: int,
    timeout_seconds: float,
    cpu_sample_fn: Callable[[Path, tuple[int, int]], object] = _sample_cpu,
    identity_fn: Callable[[tuple[int, int]], tuple[int, int]] = _process_starttimes,
) -> dict[str, object]:
    """Capture local browser and CPU time in parallel without deleting a session."""

    if (
        type(firefox_pid) is not int
        or firefox_pid <= 0
        or type(xorg_pid) is not int
        or xorg_pid <= 0
        or firefox_pid == xorg_pid
        or not evidence_dir.is_absolute()
    ):
        raise CaptureError("capture process identities or output path are invalid")
    performance_urls(fixture_index_url)
    if (
        not evidence_dir.is_dir()
        or evidence_dir.is_symlink()
        or not stat.S_ISDIR(evidence_dir.stat().st_mode)
    ):
        raise CaptureError("capture output directory is unavailable")
    local_path = evidence_dir / "browser-local-capture.json"
    cpu_path = evidence_dir / "browser-system-time.json"
    interaction_path = evidence_dir / "browser-interaction-capture.json"
    if (
        os.path.lexists(local_path)
        or os.path.lexists(cpu_path)
        or os.path.lexists(interaction_path)
    ):
        raise CaptureError("capture artifacts already exist")
    process_ids = (firefox_pid, xorg_pid)
    initial_starttimes = identity_fn(process_ids)

    # TCP connect and greeting use the transport's initial deadline. Give the
    # first browser command its own bounded budget instead of only the setup
    # time remaining on slow RISC-V guests.
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
        raise CaptureError("Firefox did not create a verified timing session")
    handles = _value(client.command("WebDriver:GetWindowHandles"))  # type: ignore[attr-defined]
    if (
        not isinstance(handles, list)
        or not handles
        or not all(isinstance(handle, str) for handle in handles)
    ):
        raise CaptureError("Firefox timing window is unavailable")

    # NewSession can consume a substantial part of Marionette's connection
    # deadline on RISC-V. Align the transport budget with the bounded capture
    # window only after the session and first window have been verified.
    client.set_timeout(timeout_seconds)  # type: ignore[attr-defined]

    sample_errors: list[BaseException] = []

    def sample_cpu() -> None:
        try:
            cpu_sample_fn(cpu_path, (firefox_pid, xorg_pid))
        except BaseException as error:
            sample_errors.append(error)

    sampler = threading.Thread(target=sample_cpu, name="browser-cpu-time-sampler")
    sampler.start()
    try:
        report = capture_local(
            client,
            fixture_index_url,
            synthetic_samples=synthetic_samples,
            timeout_seconds=timeout_seconds,
            interaction_checkpoint_fn=lambda checkpoint: _private_json(
                interaction_path, checkpoint
            ),
        )
    finally:
        sampler.join()
    if sample_errors or not cpu_path.is_file():
        raise CaptureError("Firefox/Xorg CPU time evidence is unavailable") from (
            sample_errors[0] if sample_errors else None
        )
    if identity_fn(process_ids) != initial_starttimes:
        raise CaptureError("Firefox/Xorg process identity changed during capture")
    report["firefox_pid"] = firefox_pid
    report["xorg_pid"] = xorg_pid
    report["process_starttime_ticks"] = list(initial_starttimes)
    report["cpu_artifact"] = cpu_path.name
    report["interaction_artifact"] = interaction_path.name
    _private_json(local_path, report)
    return report


def fixture_index_url_from_environment() -> str:
    raw = os.environ.get("ASTERINAS_DESKTOP_FIXTURE_URL", "")
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as error:
        raise CaptureError("desktop fixture URL is malformed") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in FIXTURE_HOSTS
        or port != 17894
        or parsed.path != "/asterinas-network-probe.bin"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CaptureError("desktop fixture URL is outside the frozen contract")
    return f"http://{parsed.hostname}:17894/browser-quality/index.html"


def resolve_fixture_index_url(explicit_url: str | None) -> str:
    """Permit an isolated root console to select an exact local fixture URL."""

    index_url = (
        explicit_url
        if explicit_url is not None
        else fixture_index_url_from_environment()
    )
    performance_urls(index_url)
    return index_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firefox-pid", type=int, required=True)
    parser.add_argument("--xorg-pid", type=int, required=True)
    parser.add_argument("--synthetic-samples", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument(
        "--evidence-dir", type=Path, default=Path("/run/asterinas-browser-perf")
    )
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--fixture-index-url")
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("Marionette port is invalid")
    try:
        options.evidence_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        index_url = resolve_fixture_index_url(options.fixture_index_url)
        client = _connect(
            "127.0.0.1",
            options.port,
            time.monotonic() + options.timeout_seconds,
        )
        try:
            report = run_capture(
                client,
                index_url,
                firefox_pid=options.firefox_pid,
                xorg_pid=options.xorg_pid,
                evidence_dir=options.evidence_dir,
                synthetic_samples=options.synthetic_samples,
                timeout_seconds=options.timeout_seconds,
            )
        finally:
            # Do not DeleteSession: it can stop the listener or the desktop.
            client.close()
    except (CaptureError, OSError, TimeoutError) as error:
        detail = (
            f" detail={json.dumps(str(error)[:120])}"
            if isinstance(error, CaptureError)
            else ""
        )
        print(
            f"ASTERINAS_BROWSER_PERF_FAIL reason={type(error).__name__}{detail}",
            file=sys.stderr,
        )
        return 1
    print(
        "ASTERINAS_BROWSER_PERF_PASS "
        f"local_total_ms={report['navigation_parts']['total_ms']} "  # type: ignore[index]
        f"evidence_dir={options.evidence_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
