#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Build a conservative report from three qualified physical daily-use runs."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics

from tools.riscv.debian.rootfs.browser_daily_use_contract import (
    DailyUseContractError,
    validate_daily_use_result,
)


PRIMARY_METRICS = (
    "keyboardFirstRafP95Ms",
    "keyboardNextRafP95Ms",
    "pointerFirstRafP95Ms",
    "pointerNextRafP95Ms",
    "scrollFirstRafP95Ms",
    "scrollNextRafP95Ms",
    "navigationCommandMs",
    "navigationResponseToDomMs",
    "contextSwitchTotalMs",
)
ATTRIBUTION_METRICS = (
    "firefoxCpuSeconds",
    "xorgCpuSeconds",
    "mainThreadRuntimeSeconds",
    "mainThreadRunqueueWaitSeconds",
    "mainThreadDispatches",
    "systemUtilization",
    "contextSwitches",
    "runnableSamples",
)
MAX_FILE_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
REQUIRED_FILES = frozenset(
    {
        "run-result.json",
        "deployment.json",
        "fixture-summary.json",
        "serial.log",
        "browser-daily-use-result.json",
        "browser-composite-capture.json",
        "browser-system-time.json",
        "browser-thread-time.json",
    }
)


class ReportError(ValueError):
    """One input run or cross-run admission rule is invalid."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _read_regular(directory_fd: int, name: str) -> bytes:
    if Path(name).name != name or not name or name in (".", ".."):
        raise ReportError("manifest file name is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise ReportError(f"cannot open manifested file {name}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o077
            or before.st_size > MAX_FILE_BYTES
        ):
            raise ReportError(f"manifested file {name} is not bounded regular data")
        payload = bytearray()
        while len(payload) <= MAX_FILE_BYTES:
            chunk = os.read(
                descriptor, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(payload))
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(payload) != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in identity
        ):
            raise ReportError(f"manifested file {name} changed while reading")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _json(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportError(f"{label} is not valid JSON") from error
    if type(value) is not dict:
        raise ReportError(f"{label} is not a JSON object")
    return value


def _verified_files(directory: Path) -> dict[str, bytes]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise ReportError("cannot open run directory") from error
    try:
        manifest_raw = _read_regular(directory_fd, "sha256-manifest.json")
        manifest = _json(manifest_raw, "SHA-256 manifest")
        if _canonical_json(manifest) != manifest_raw:
            raise ReportError("SHA-256 manifest is not canonical")
        if (
            set(manifest) != {"schemaVersion", "files"}
            or manifest["schemaVersion"] != 1
        ):
            raise ReportError("SHA-256 manifest schema is invalid")
        rows = manifest["files"]
        if type(rows) is not list or not rows:
            raise ReportError("SHA-256 manifest file list is invalid")
        payloads: dict[str, bytes] = {}
        for row in rows:
            if type(row) is not dict or set(row) != {"name", "bytes", "sha256"}:
                raise ReportError("SHA-256 manifest row is invalid")
            name = row["name"]
            if (
                not isinstance(name, str)
                or name in payloads
                or name == "sha256-manifest.json"
            ):
                raise ReportError("SHA-256 manifest file identity is invalid")
            payload = _read_regular(directory_fd, name)
            digest = hashlib.sha256(payload).hexdigest()
            if (
                type(row["bytes"]) is not int
                or row["bytes"] != len(payload)
                or not isinstance(row["sha256"], str)
                or SHA256.fullmatch(row["sha256"]) is None
                or row["sha256"] != digest
            ):
                raise ReportError(f"manifest size or digest mismatch for {name}")
            payloads[name] = payload
        actual = {
            entry.name
            for entry in os.scandir(directory)
            if entry.name != "sha256-manifest.json"
        }
        if actual != set(payloads):
            raise ReportError("SHA-256 manifest does not cover the run directory")
        if not REQUIRED_FILES <= set(payloads):
            raise ReportError("qualified run is missing required files")
        return payloads
    finally:
        os.close(directory_fd)


def _numeric(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ReportError(f"{label} is outside its bound")
    return result


def _phase_duration(phase: dict[str, object]) -> float:
    start = _numeric(phase["startMs"], "phase start")
    end = _numeric(phase["endMs"], "phase end")
    if end < start:
        raise ReportError("phase duration is negative")
    return end - start


def _performance_map(result: dict[str, object]) -> dict[str, dict[str, object]]:
    performance = result["performance"]
    assert isinstance(performance, list)
    return {str(item["name"]): item for item in performance}


def _primary_metrics(result: dict[str, object]) -> dict[str, float | None]:
    phases = _performance_map(result)
    input_metrics = phases["input"]["metrics"]
    scroll = phases["scroll"]["metrics"]
    navigation = phases["navigation"]["metrics"]
    context = phases["context-switch"]["metrics"]
    assert isinstance(input_metrics, dict)
    assert isinstance(scroll, dict)
    assert isinstance(navigation, dict)
    assert isinstance(context, dict)
    keyboard = input_metrics["keyboard"]
    pointer = input_metrics["pointer"]
    assert isinstance(keyboard, dict) and isinstance(pointer, dict)
    values = {
        "keyboardFirstRafP95Ms": keyboard["firstRaf"]["p95Ms"],
        "keyboardNextRafP95Ms": keyboard["nextRaf"]["p95Ms"],
        "pointerFirstRafP95Ms": pointer["firstRaf"]["p95Ms"],
        "pointerNextRafP95Ms": pointer["nextRaf"]["p95Ms"],
        "scrollFirstRafP95Ms": scroll["firstRaf"]["p95Ms"],
        "scrollNextRafP95Ms": scroll["nextRaf"]["p95Ms"],
        "contextSwitchTotalMs": context["totalMs"],
    }
    primary: dict[str, float | None] = {
        name: _numeric(value, name) for name, value in values.items()
    }
    if phases["navigation"]["state"] == "unsupported":
        primary.update(
            navigationCommandMs=None,
            navigationResponseToDomMs=None,
        )
    else:
        try:
            local = navigation["localCommand"]
            browser = navigation["browserNavigation"]
            assert isinstance(local, dict) and isinstance(browser, dict)
            primary.update(
                navigationCommandMs=_numeric(
                    local["durationMs"], "navigationCommandMs"
                ),
                navigationResponseToDomMs=_numeric(
                    browser["responseToDomMs"], "navigationResponseToDomMs"
                ),
            )
        except (AssertionError, KeyError) as error:
            raise ReportError("supported navigation metrics are incomplete") from error
    return {name: primary[name] for name in PRIMARY_METRICS}


def _intervals(value: dict[str, object], label: str) -> list[dict[str, object]]:
    intervals = value.get("intervals")
    if (
        type(intervals) is not list
        or not intervals
        or any(type(row) is not dict for row in intervals)
    ):
        raise ReportError(f"{label} intervals are invalid")
    return intervals


def _coverage(
    composite: dict[str, object],
    system: dict[str, object],
    thread: dict[str, object],
) -> tuple[int, int]:
    start = composite.get("workload_start_observed_guest_monotonic_ns")
    observations = composite.get("phase_observations")
    if type(start) is not int or type(observations) is not list or not observations:
        raise ReportError("workload coverage markers are invalid")
    final = observations[-1]
    if (
        type(final) is not dict
        or type(final.get("observed_guest_monotonic_ns")) is not int
    ):
        raise ReportError("workload coverage endpoint is invalid")
    end = final["observed_guest_monotonic_ns"]
    for label, artifact in (("system", system), ("thread", thread)):
        rows = _intervals(artifact, label)
        first = rows[0].get("guest_monotonic_start_ns")
        last = rows[-1].get("guest_monotonic_end_ns")
        if (
            type(first) is not int
            or type(last) is not int
            or first > start
            or last < end
        ):
            raise ReportError(f"{label} sampler coverage is incomplete")
    return start, end


def _process_attribution(
    system: dict[str, object], firefox_pid: int, xorg_pid: int
) -> tuple[dict[str, float], list[dict[str, object]]]:
    firefox_ms = 0.0
    xorg_ms = 0.0
    wall_ms = 0.0
    context_switches = 0.0
    busy: list[float] = []
    rows = _intervals(system, "system")
    for interval in rows:
        wall_ms += _numeric(interval.get("duration_ms"), "profile wall duration")
        processes = interval.get("processes")
        system_row = interval.get("system")
        if type(processes) is not list or type(system_row) is not dict:
            raise ReportError("system sampler row is incomplete")
        for process in processes:
            if type(process) is not dict or type(process.get("pid")) is not int:
                raise ReportError("process sampler row is invalid")
            cpu_ms = _numeric(
                process.get("cpu_user_ms"), "process user CPU"
            ) + _numeric(process.get("cpu_kernel_ms"), "process kernel CPU")
            if process["pid"] == firefox_pid:
                firefox_ms += cpu_ms
            elif process["pid"] == xorg_pid:
                xorg_ms += cpu_ms
        context_switches += _numeric(
            system_row.get("context_switches"), "context switches"
        )
        per_cpu = system_row.get("per_cpu")
        if type(per_cpu) is not list:
            raise ReportError("per-CPU utilization is unavailable")
        for cpu in per_cpu:
            if type(cpu) is not dict or cpu.get("busy_fraction") is None:
                raise ReportError("per-CPU utilization is invalid")
            fraction = _numeric(cpu["busy_fraction"], "system utilization")
            if fraction > 1:
                raise ReportError("system utilization exceeds one")
            busy.append(fraction)
    if wall_ms <= 0 or not busy:
        raise ReportError("profile wall coverage is invalid")
    return (
        {
            "firefoxCpuSeconds": firefox_ms / 1000.0,
            "xorgCpuSeconds": xorg_ms / 1000.0,
            "profileWallSeconds": wall_ms / 1000.0,
            "systemUtilization": statistics.mean(busy),
            "contextSwitches": context_switches,
        },
        rows,
    )


def _thread_attribution(
    thread: dict[str, object], firefox_pid: int
) -> tuple[dict[str, float | int | bool], list[dict[str, object]]]:
    aggregates: dict[int, dict[str, int]] = {}
    rows = _intervals(thread, "thread")
    runnable_samples = 0
    for interval in rows:
        threads = interval.get("threads")
        if type(threads) is not list:
            raise ReportError("thread sampler row is incomplete")
        main_runnable = False
        for item in threads:
            if type(item) is not dict or type(item.get("tid")) is not int:
                raise ReportError("thread sampler entry is invalid")
            schedstat = item.get("schedstat")
            delta = schedstat.get("delta") if type(schedstat) is dict else None
            if type(delta) is not dict:
                raise ReportError("thread schedstat delta is unavailable")
            runtime = delta.get("cpu_runtime_ns")
            wait = delta.get("runqueue_wait_ns")
            dispatches = delta.get("dispatch_count")
            if any(
                type(value) is not int or value < 0
                for value in (runtime, wait, dispatches)
            ):
                raise ReportError("thread schedstat delta is invalid")
            aggregate = aggregates.setdefault(
                item["tid"], {"runtime": 0, "wait": 0, "dispatches": 0}
            )
            aggregate["runtime"] += runtime
            aggregate["wait"] += wait
            aggregate["dispatches"] += dispatches
            if item["tid"] == firefox_pid and wait > 0:
                main_runnable = True
        runnable_samples += int(main_runnable)
    if firefox_pid not in aggregates:
        raise ReportError("Firefox process leader is missing from thread evidence")
    hottest_tid = max(
        aggregates,
        key=lambda tid: (aggregates[tid]["runtime"], -tid),
    )
    main = aggregates[firefox_pid]
    return (
        {
            "mainThreadRuntimeSeconds": main["runtime"] / 1_000_000_000,
            "mainThreadRunqueueWaitSeconds": main["wait"] / 1_000_000_000,
            "mainThreadDispatches": main["dispatches"],
            "runnableSamples": runnable_samples,
            "hottestTid": hottest_tid,
            "hottestIsLeader": hottest_tid == firefox_pid,
        },
        rows,
    )


def _classify(
    primary: dict[str, float | None], attribution: dict[str, float | int | bool]
) -> tuple[str, dict[str, float]]:
    runtime = float(attribution["mainThreadRuntimeSeconds"])
    wait = float(attribution["mainThreadRunqueueWaitSeconds"])
    total = float(attribution["firefoxCpuSeconds"])
    wall = float(attribution["profileWallSeconds"])
    affected = max(value for value in primary.values() if value is not None)
    wait_ratio = wait / max(runtime + wait, 1e-9)
    main_runtime_share = runtime / max(total, 1e-9)
    occupancy = total / max(wall, 1e-9)
    unaccounted = max(wall - total, 0.0) / max(wall, 1e-9)
    if wait_ratio >= 0.20 and wait * 1000 >= affected:
        classification = "runnable-delayed"
    elif main_runtime_share >= 0.50 and occupancy >= 0.50 and wait_ratio < 0.20:
        classification = "executing"
    elif unaccounted >= 0.50 and occupancy < 0.25 and wait_ratio < 0.20:
        classification = "sleeping-blocking"
    else:
        classification = "mixed"
    return classification, {
        "affectedPrimaryMs": affected,
        "waitRatio": wait_ratio,
        "mainRuntimeShare": main_runtime_share,
        "processCpuOccupancy": occupancy,
        "unaccountedRatio": unaccounted,
    }


def _run_identity(
    run_result: dict[str, object], deployment: dict[str, object]
) -> dict[str, object]:
    artifacts = deployment.get("artifacts")
    if type(artifacts) is not dict:
        raise ReportError("deployment artifact identities are invalid")
    try:
        return {
            "commit": deployment["commit"],
            "kernel": artifacts["kernel"],
            "stage1": artifacts["initramfs"],
            "dtb": artifacts["megrez_dtb"],
            "rootfsManifest": artifacts["root_manifest"],
            "boardSerial": deployment["serialDevice"],
            "hartCount": deployment["hartCount"],
            "fixture": run_result["fixture"],
            "displayProvider": deployment["displayProvider"],
            "browserPackage": deployment["browserPackage"],
            "bootargsSha256": run_result["bootargs_sha256"],
        }
    except KeyError as error:
        raise ReportError("deployment immutable identity is incomplete") from error


def _load_run(directory: Path) -> dict[str, object]:
    payloads = _verified_files(directory)
    run_result = _json(payloads["run-result.json"], "run result")
    if (
        run_result.get("schema_version") != 1
        or run_result.get("passed") is not True
        or run_result.get("qualified") is not True
        or run_result.get("recovered") is not True
    ):
        raise ReportError("report input is not a qualified recovered run")
    experiment_id = run_result.get("experiment_id")
    gate_run_id = run_result.get("gate_run_id")
    if (
        not isinstance(experiment_id, str)
        or RUN_ID.fullmatch(experiment_id) is None
        or not isinstance(gate_run_id, str)
        or RUN_ID.fullmatch(gate_run_id) is None
    ):
        raise ReportError("run identities are invalid")
    try:
        daily_result = validate_daily_use_result(
            json.loads(payloads["browser-daily-use-result.json"])
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DailyUseContractError) as error:
        raise ReportError("daily-use result contract is invalid") from error
    if daily_result["runId"] != gate_run_id or daily_result["state"] != "pass":
        raise ReportError("daily-use gate identity or state disagrees")
    terminal = run_result.get("terminal")
    if terminal != {
        "experiment_id": experiment_id,
        "outcome": "pass",
        "gate_status": 0,
        "upload_status": 0,
    }:
        raise ReportError("qualified run terminal evidence disagrees")
    for artifact in daily_result["artifacts"]:
        name = artifact["name"]
        if name not in payloads:
            raise ReportError("daily-use artifact is absent from the run directory")
        payload = payloads[name]
        if (
            artifact["bytes"] != len(payload)
            or artifact["sha256"] != hashlib.sha256(payload).hexdigest()
        ):
            raise ReportError("daily-use artifact identity disagrees")
    for row in run_result.get("artifacts", []):
        if type(row) is not dict or row.get("name") not in payloads:
            raise ReportError("run artifact declaration is invalid")
        payload = payloads[row["name"]]
        if (
            row.get("bytes") != len(payload)
            or row.get("sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise ReportError("run artifact identity disagrees")
    composite = _json(payloads["browser-composite-capture.json"], "composite artifact")
    system = _json(payloads["browser-system-time.json"], "system artifact")
    thread = _json(payloads["browser-thread-time.json"], "thread artifact")
    _coverage(composite, system, thread)
    if composite.get("run_id") != gate_run_id:
        raise ReportError("composite gate identity disagrees")
    identities = daily_result["identities"]
    firefox_pid = identities["firefox"]["initial"]["pid"]
    xorg_pid = identities["xorg"]["initial"]["pid"]
    if thread.get("process_id") != firefox_pid:
        raise ReportError("thread sampler Firefox identity disagrees")
    primary = _primary_metrics(daily_result)
    process, process_rows = _process_attribution(system, firefox_pid, xorg_pid)
    threads, thread_rows = _thread_attribution(thread, firefox_pid)
    attribution = {**process, **threads}
    classification, ratios = _classify(primary, attribution)
    deployment = _json(payloads["deployment.json"], "deployment")
    embedded_deployment = run_result.get("deployment")
    if type(embedded_deployment) is not dict or any(
        deployment.get(key) != value for key, value in embedded_deployment.items()
    ):
        raise ReportError("deployment evidence disagrees")
    workload = composite.get("workload")
    phases = workload.get("phases", []) if type(workload) is dict else []
    phase_durations = []
    if type(phases) is list:
        for phase in phases:
            if type(phase) is dict and "startMs" in phase and "endMs" in phase:
                phase_durations.append(
                    {
                        "name": phase.get("name"),
                        "durationMs": _phase_duration(phase),
                    }
                )
    threshold_crossings = [
        item["name"] for item in daily_result["performance"] if item["state"] == "slow"
    ]
    performance = _performance_map(daily_result)
    return {
        "path": str(directory),
        "experimentId": experiment_id,
        "gateRunId": gate_run_id,
        "identity": _run_identity(run_result, deployment),
        "primary": primary,
        "attribution": attribution,
        "ratios": ratios,
        "classification": classification,
        "diagnosticThresholdCrossings": threshold_crossings,
        "functionGroups": daily_result["functionGroups"],
        "dailyUseLimitations": daily_result["limitations"],
        "raw": {
            "keyboard": performance["input"]["metrics"]["keyboard"],
            "pointer": performance["input"]["metrics"]["pointer"],
            "scroll": performance["scroll"]["metrics"],
            "navigation": performance["navigation"]["metrics"],
            "contextSwitch": performance["context-switch"]["metrics"],
            "phaseDurations": phase_durations,
            "processSamples": process_rows,
            "threadSamples": thread_rows,
        },
    }


def _summary(values: list[float | None]) -> dict[str, object]:
    supported = [value for value in values if value is not None]
    is_complete = len(supported) == len(values)
    return {
        "values": values,
        "supportedRuns": len(supported),
        "median": statistics.median(supported) if is_complete else None,
        "minimum": min(supported) if is_complete else None,
        "maximum": max(supported) if is_complete else None,
    }


def build_report(run_directories: Sequence[Path]) -> dict[str, object]:
    """Validate and summarize exactly three immutable qualified runs."""

    if len(run_directories) != 3 or any(
        not isinstance(path, Path) for path in run_directories
    ):
        raise ReportError("report requires three distinct run directories")
    absolute = [path.absolute() for path in run_directories]
    if len(set(absolute)) != 3:
        raise ReportError("report requires three distinct run directories")
    runs = [_load_run(path) for path in absolute]
    experiments = {run["experimentId"] for run in runs}
    gate_runs = {run["gateRunId"] for run in runs}
    if len(experiments) != 3 or len(gate_runs) != 3:
        raise ReportError(
            "report requires three distinct experiment and gate identities"
        )
    identity = runs[0]["identity"]
    if any(run["identity"] != identity for run in runs[1:]):
        raise ReportError("qualified runs have mixed immutable identities")
    daily_use_coverage = {
        "functionGroups": runs[0]["functionGroups"],
        "limitations": runs[0]["dailyUseLimitations"],
    }
    if any(
        run["functionGroups"] != daily_use_coverage["functionGroups"]
        or run["dailyUseLimitations"] != daily_use_coverage["limitations"]
        for run in runs[1:]
    ):
        raise ReportError("qualified runs have mixed daily-use capability coverage")
    metrics = {
        name: _summary([run["primary"][name] for run in runs])
        for name in PRIMARY_METRICS
    }
    metrics.update(
        {
            name: _summary([float(run["attribution"][name]) for run in runs])
            for name in ATTRIBUTION_METRICS
        }
    )
    classifications = [str(run["classification"]) for run in runs]
    counts = Counter(classifications)
    agreement = max(counts.values())
    leader_every_run = all(
        run["attribution"]["hottestIsLeader"] is True for run in runs
    )
    admitted = (
        classifications[0]
        if len(counts) == 1 and classifications[0] != "mixed" and leader_every_run
        else "mixed"
    )
    return {
        "schemaVersion": 1,
        "runCount": 3,
        "classification": admitted,
        "classificationAgreement": agreement,
        "leaderHottestInEveryRun": leader_every_run,
        "admissionRule": "same-non-mixed-mechanism-in-three-runs-and-leader-hottest",
        "thresholds": {
            "runnableWaitRatio": 0.20,
            "executingMainRuntimeShare": 0.50,
            "executingProcessCpuOccupancy": 0.50,
            "sleepingUnaccountedRatio": 0.50,
            "sleepingMaximumProcessCpuOccupancy": 0.25,
        },
        "diagnosticThresholdRuns": sum(
            bool(run["diagnosticThresholdCrossings"]) for run in runs
        ),
        "immutableIdentity": identity,
        "dailyUseCoverage": daily_use_coverage,
        "metrics": metrics,
        "runs": runs,
        "limitations": [
            "mechanism-class-admission-is-not-proof-of-a-specific-function-or-subsystem",
            "browser-timings-are-not-usb-to-hdmi-latency",
            "procfs-placeholder-fault-fields-were-not-used",
            "primary-metric-aggregates-require-three-supported-runs",
        ],
    }


def _markdown(report: dict[str, object], inputs: Sequence[Path]) -> bytes:
    def format_metric(value: float | None) -> str:
        return "unsupported" if value is None else f"{value:.3f}"

    metrics = report["metrics"]
    runs = report["runs"]
    lines = [
        "# Firefox daily-use physical baseline",
        "",
        "## Scope",
        "",
        "Exactly three qualified, recovered, one-profile-per-boot Megrez runs were admitted.",
        "Browser timings are not USB-to-HDMI latency, and procfs placeholder fault fields were not used.",
        "",
        "## Immutable identities",
        "",
        "```json",
        json.dumps(report["immutableIdentity"], sort_keys=True, indent=2),
        "```",
        "",
        "## Daily-use capability coverage",
        "",
    ]
    lines.extend(
        f"- {item['name']}: {item['state']}"
        for item in report["dailyUseCoverage"]["functionGroups"]
    )
    lines.extend(["", "Daily-use limitations:", ""])
    daily_use_limitations = report["dailyUseCoverage"]["limitations"]["items"]
    lines.extend(f"- {item}" for item in (daily_use_limitations or ["none"]))
    lines.extend(
        [
            "",
            "## Primary metrics (ms)",
            "",
            "| Metric | Run 1 | Run 2 | Run 3 | Supported | Median | Min | Max |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in PRIMARY_METRICS:
        summary = metrics[name]
        values = summary["values"]
        lines.append(
            f"| {name} | {format_metric(values[0])} | {format_metric(values[1])} | "
            f"{format_metric(values[2])} | {summary['supportedRuns']}/3 | "
            f"{format_metric(summary['median'])} | {format_metric(summary['minimum'])} | "
            f"{format_metric(summary['maximum'])} |"
        )
    lines.extend(
        [
            "",
            "## Attribution and admission",
            "",
            f"Classification: **{report['classification']}** "
            f"({report['classificationAgreement']}/3 per-run agreement).",
            "This is a mechanism-class admission rule, not proof of a specific function or subsystem.",
            "",
            "| Run | Classification | Wait ratio | Main runtime share | CPU occupancy | Unaccounted |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for index, run in enumerate(runs, start=1):
        ratios = run["ratios"]
        lines.append(
            f"| {index} | {run['classification']} | {ratios['waitRatio']:.4f} | "
            f"{ratios['mainRuntimeShare']:.4f} | {ratios['processCpuOccupancy']:.4f} | "
            f"{ratios['unaccountedRatio']:.4f} |"
        )
    lines.extend(["", "## Inputs and hashes", ""])
    for path in inputs:
        manifest = path / "sha256-manifest.json"
        lines.append(
            f"- `{path}` — `{hashlib.sha256(manifest.read_bytes()).hexdigest()}`"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return ("\n".join(lines) + "\n").encode()


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    """Write exclusive canonical JSON and Markdown reports."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs=3)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    options = parser.parse_args(argv)
    if options.json_output.exists() or options.markdown_output.exists():
        raise FileExistsError("report output already exists")
    report = build_report(options.runs)
    _write_exclusive(options.json_output, _canonical_json(report))
    try:
        _write_exclusive(options.markdown_output, _markdown(report, options.runs))
    except BaseException:
        options.json_output.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
