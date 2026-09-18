#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run one bounded physical Firefox daily-use profile on Megrez."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
from typing import Protocol

from tools.riscv.debian.rootfs.browser_daily_use_contract import (
    FUNCTION_GROUPS,
    DailyUseContractError,
    validate_daily_use_result,
)
from tools.riscv.debian.rootfs.browser_daily_use_upload import (
    EvidenceBundle,
    parse_bundle,
)
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_debug_contract import DebugPlan
from tools.riscv.megrez_network_fixture import (
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
)
from tools.riscv.megrez_physical_graphics import (
    DisplayEvidenceMode,
    DailyUseTerminalStatus,
    GraphicalReadinessEvidence,
    HostGateError,
    RealPhysicalGraphicsOperations,
    _read_plan,
    _validate_physical_artifacts,
    physical_bootargs,
)


FIXTURE_BIND = "10.100.19.216"
FIXTURE_PORT = 17894
BOARD_PEER = "10.100.19.200"
FIXTURE_SOURCE_URL = "http://10.100.19.216:17894/asterinas-network-probe.bin"
EXPERIMENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
RESULT_NAME = "browser-daily-use-result.json"
COMPOSITE_NAME = "browser-composite-capture.json"
SYSTEM_NAME = "browser-system-time.json"
THREAD_NAME = "browser-thread-time.json"


def _bounded_deadline(value: object, label: str, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{label} must be in (0, {maximum:g}]")
    return float(value)


@dataclass(frozen=True)
class DailyUsePhysicalConfig:
    """Closed physical addresses, paths, and deadlines for one profile."""

    device: Path
    output_directory: Path
    fixture_bind: str = FIXTURE_BIND
    fixture_port: int = FIXTURE_PORT
    board_peer: str = BOARD_PEER
    profile_timeout: float = 120.0
    open_timeout: float = 60.0
    artifact_timeout: float = 300.0
    boot_timeout: float = 300.0
    upload_timeout: float = 30.0
    recovery_timeout: float = 930.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.device, Path)
            or not self.device.is_absolute()
            or not str(self.device).startswith("/dev/serial/by-id/")
        ):
            raise ValueError("device must be an absolute /dev/serial/by-id path")
        if (
            not isinstance(self.output_directory, Path)
            or not self.output_directory.is_absolute()
        ):
            raise ValueError("output directory must be absolute")
        if (
            self.fixture_bind != FIXTURE_BIND
            or type(self.fixture_port) is not int
            or self.fixture_port != FIXTURE_PORT
            or self.board_peer != BOARD_PEER
        ):
            raise ValueError("physical daily-use network identity is fixed")
        profile = _bounded_deadline(self.profile_timeout, "profile timeout", 120)
        if int(profile) != profile:
            raise ValueError("profile timeout must be an integer number of seconds")
        _bounded_deadline(self.open_timeout, "open timeout", 1200)
        _bounded_deadline(self.artifact_timeout, "artifact timeout", 1200)
        _bounded_deadline(self.boot_timeout, "boot timeout", 1200)
        _bounded_deadline(self.upload_timeout, "upload timeout", 300)
        _bounded_deadline(self.recovery_timeout, "recovery timeout", 1200)


@dataclass(frozen=True)
class DailyUsePhysicalResult:
    """Canonical cross-channel verdict for one physical daily-use run."""

    schema_version: int
    experiment_id: str
    gate_run_id: str | None
    passed: bool
    qualified: bool
    reason: str
    recovered: bool
    plan_sha256: str
    bootargs_sha256: str
    fixture: dict[str, object]
    deployment: dict[str, object]
    terminal: dict[str, object] | None
    artifacts: tuple[dict[str, object], ...]

    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))


class DailyUsePhysicalOperations(Protocol):
    """Board, serial, recovery, and publication operations used by the runner."""

    @property
    def transcript(self) -> str: ...

    @property
    def guest_started(self) -> bool: ...

    def invalidate(self) -> None: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]: ...

    def boot(self, plan: DebugPlan, bootargs: str, timeout: float) -> None: ...

    def prove_graphical_readiness(
        self, timeout: float
    ) -> GraphicalReadinessEvidence: ...

    def run_daily_use_profile(
        self, experiment_id: str, timeout: float, expected_firefox_pid: int
    ) -> DailyUseTerminalStatus: ...

    def request_reboot(self, timeout: float) -> None: ...

    def await_recovery(self, timeout: float) -> None: ...

    def publish_daily_use(
        self,
        result: DailyUsePhysicalResult,
        bundle: EvidenceBundle | None,
        transcript: str,
        transport: tuple[str, ...],
        fixture_summary: dict[str, object] | None,
    ) -> None: ...

    def close(self) -> None: ...


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def daily_use_physical_bootargs(plan: DebugPlan) -> str:
    """Derive the fixed fixture-only physical daily-use boot arguments."""

    tokens = physical_bootargs(plan).split()
    loopback_proxy = "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1"
    fixture_proxy = f"systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST={FIXTURE_BIND}"
    if tokens.count(loopback_proxy) != 1:
        raise HostGateError("normal physical proxy identity is unavailable")
    tokens[tokens.index(loopback_proxy)] = fixture_proxy
    separator = tokens.index("--")
    daily_tokens = (
        f"asterinas.net=eic7700-rj45,{BOARD_PEER}/21,10.100.16.1",
        "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e",
        "systemd.setenv=ASTERINAS_PHYSICAL_DAILY_USE=1",
        f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL={FIXTURE_SOURCE_URL}",
    )
    tokens[separator:separator] = daily_tokens
    if any(tokens.count(token) != 1 for token in daily_tokens):
        raise HostGateError("daily-use boot argument identity is ambiguous")
    return " ".join(tokens)


def _json_artifact(bundle: EvidenceBundle, name: str) -> dict[str, object]:
    try:
        value = json.loads(bundle.artifacts[name])
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostGateError(f"daily-use artifact {name} is invalid") from error
    if type(value) is not dict:
        raise HostGateError(f"daily-use artifact {name} is invalid")
    return value


def _sampler_covers(
    sampler: dict[str, object], workload_start: int, workload_end: int
) -> bool:
    intervals = sampler.get("intervals")
    if type(intervals) is not list or not intervals:
        return False
    first = intervals[0]
    last = intervals[-1]
    return (
        type(first) is dict
        and type(last) is dict
        and type(first.get("guest_monotonic_start_ns")) is int
        and type(last.get("guest_monotonic_end_ns")) is int
        and first["guest_monotonic_start_ns"] <= workload_start
        and last["guest_monotonic_end_ns"] >= workload_end
    )


def _bundle_predicates(
    bundle: EvidenceBundle | None,
) -> tuple[dict[str, bool], dict[str, object] | None]:
    predicates = {
        "bundle-pass": False,
        "result-pass": False,
        "function-groups-pass": False,
        "identities-stable": False,
        "attribution-complete": False,
        "sampler-coverage": False,
        "gate-run-match": False,
    }
    if bundle is None or bundle.outcome != "pass":
        return predicates, None
    predicates["bundle-pass"] = True
    try:
        result = validate_daily_use_result(json.loads(bundle.artifacts[RESULT_NAME]))
        composite = _json_artifact(bundle, COMPOSITE_NAME)
        system = _json_artifact(bundle, SYSTEM_NAME)
        thread = _json_artifact(bundle, THREAD_NAME)
    except (
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DailyUseContractError,
        HostGateError,
    ):
        return predicates, None
    predicates["result-pass"] = result["state"] == "pass"
    groups = result["functionGroups"]
    predicates["function-groups-pass"] = tuple(
        item["name"] for item in groups
    ) == FUNCTION_GROUPS and all(item["state"] == "pass" for item in groups)
    identities = result["identities"]
    predicates["identities-stable"] = all(
        identities[name]["initial"] == identities[name]["final"]
        for name in ("firefox", "xorg")
    )
    attribution = result["attribution"]
    predicates["attribution-complete"] = (
        attribution.get("systemArtifact") == SYSTEM_NAME
        and attribution.get("threadArtifact") == THREAD_NAME
        and attribution.get("compositeArtifact") == COMPOSITE_NAME
    )
    observations = composite.get("phase_observations")
    start = composite.get("workload_start_observed_guest_monotonic_ns")
    if type(observations) is list and observations and type(start) is int:
        final = observations[-1]
        if (
            type(final) is dict
            and type(final.get("observed_guest_monotonic_ns")) is int
        ):
            end = final["observed_guest_monotonic_ns"]
            predicates["sampler-coverage"] = _sampler_covers(
                system, start, end
            ) and _sampler_covers(thread, start, end)
    predicates["gate-run-match"] = result["runId"] == bundle.gate_run_id
    return predicates, result


def _plan_deployment(plan: DebugPlan) -> dict[str, object]:
    artifacts = {
        identity.name: identity.sha256
        for identity in plan.artifacts
        if hasattr(identity, "name") and hasattr(identity, "sha256")
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    packages_lock = artifacts.get("packages_lock", "unknown")
    return {
        "commit": commit,
        "profile": getattr(plan, "profile", None),
        "smp": getattr(plan, "smp", None),
        "hartCount": getattr(plan, "smp", None),
        "sv39": getattr(plan, "sv39", None),
        "artifacts": artifacts,
        "displayProvider": "fbdev",
        "browserPackage": f"firefox-esr@packages-lock:{packages_lock}",
    }


def _failure_reason(error: BaseException) -> str:
    message = str(error).strip().replace("\n", " ")
    return f"{type(error).__name__}:{message or 'unspecified'}"


def _build_result(
    plan: DebugPlan,
    bootargs: str,
    experiment_id: str,
    terminal: DailyUseTerminalStatus | None,
    bundle: EvidenceBundle | None,
    recovered: bool,
    failure: BaseException | None,
) -> DailyUsePhysicalResult:
    """Derive pass and qualification only from validated cross-channel evidence."""

    bundle_checks, _daily_result = _bundle_predicates(bundle)
    checks = {
        "terminal-pass": (
            terminal is not None
            and terminal.experiment_id == experiment_id
            and terminal.outcome == "pass"
            and terminal.gate_status == 0
            and terminal.upload_status == 0
        ),
        **bundle_checks,
        "experiment-match": (
            bundle is not None and bundle.experiment_id == experiment_id
        ),
        "channel-outcome-match": (
            terminal is not None
            and bundle is not None
            and terminal.outcome == bundle.outcome
        ),
        "recovered": recovered,
        "no-runtime-failure": failure is None,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failure is not None:
        failed.append(f"failure={_failure_reason(failure)}")
    qualified = not failed
    artifact_rows: tuple[dict[str, object], ...] = ()
    if bundle is not None:
        artifact_rows = tuple(
            {
                "name": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in bundle.artifacts.items()
        )
    return DailyUsePhysicalResult(
        schema_version=1,
        experiment_id=experiment_id,
        gate_run_id=None if bundle is None else bundle.gate_run_id,
        passed=qualified,
        qualified=qualified,
        reason="qualified" if qualified else ";".join(failed),
        recovered=recovered,
        plan_sha256=plan.plan_sha256,
        bootargs_sha256=hashlib.sha256(bootargs.encode()).hexdigest(),
        fixture={
            "bindAddress": FIXTURE_BIND,
            "port": FIXTURE_PORT,
            "allowedPeer": BOARD_PEER,
            "payloadBytes": PAYLOAD_SIZE,
            "payloadSha256": PAYLOAD_SHA256,
        },
        deployment=_plan_deployment(plan),
        terminal=None if terminal is None else asdict(terminal),
        artifacts=artifact_rows,
    )


def _validate_run_inputs(
    plan: DebugPlan,
    config: DailyUsePhysicalConfig,
    experiment_id: str,
    artifact_validator: Callable[[DebugPlan], object],
) -> str:
    if EXPERIMENT_ID_PATTERN.fullmatch(experiment_id) is None:
        raise HostGateError("physical daily-use experiment identity is invalid")
    if config.output_directory.exists() or config.output_directory.is_symlink():
        raise HostGateError("physical daily-use output already exists")
    plan.validate()
    artifact_validator(plan)
    return daily_use_physical_bootargs(plan)


def run_firefox_daily_use(
    plan: DebugPlan,
    config: DailyUsePhysicalConfig,
    operations: DailyUsePhysicalOperations,
    *,
    experiment_id: str | None = None,
    fixture_factory: Callable[[FixtureConfig], FixtureServer] = FixtureServer,
    artifact_validator: Callable[[DebugPlan], object] = _validate_physical_artifacts,
) -> DailyUsePhysicalResult:
    """Run one boot and one profile, always attempting recovery and publication."""

    selected_id = experiment_id or secrets.token_hex(16)
    bootargs = _validate_run_inputs(plan, config, selected_id, artifact_validator)
    fixture = fixture_factory(
        FixtureConfig(
            bind_address=config.fixture_bind,
            port=config.fixture_port,
            allowed_peer=config.board_peer,
            daily_use_experiment_id=selected_id,
        )
    )
    terminal: DailyUseTerminalStatus | None = None
    bundle: EvidenceBundle | None = None
    transport: tuple[str, ...] = ()
    failure: BaseException | None = None
    interruption: BaseException | None = None
    recovered = False
    try:
        try:
            operations.invalidate()
            fixture.start()
            operations.open(config.open_timeout)
            transport = operations.ensure_artifacts(plan, config.artifact_timeout)
            operations.boot(plan, bootargs, config.boot_timeout)
            readiness = operations.prove_graphical_readiness(config.boot_timeout)
            terminal = operations.run_daily_use_profile(
                selected_id, config.profile_timeout, readiness.browser_pid
            )
            raw_bundle = fixture.wait_for_daily_use_evidence(config.upload_timeout)
            bundle = parse_bundle(raw_bundle, selected_id)
        except Exception as error:
            failure = error
        except BaseException as error:
            interruption = error

        if operations.guest_started:
            try:
                operations.request_reboot(min(config.recovery_timeout, 30.0))
            except Exception:
                pass
            except BaseException as error:
                if interruption is None:
                    interruption = error
            try:
                operations.await_recovery(config.recovery_timeout)
                recovered = True
            except Exception as error:
                if failure is None:
                    failure = error
                else:
                    failure = HostGateError(
                        f"{_failure_reason(failure)}; recovery={_failure_reason(error)}"
                    )
            except BaseException as error:
                if interruption is None:
                    interruption = error

        if interruption is not None and failure is None:
            failure = HostGateError("physical daily-use run was interrupted")
        result = _build_result(
            plan, bootargs, selected_id, terminal, bundle, recovered, failure
        )
        result = replace(
            result,
            deployment={
                **result.deployment,
                "serialDevice": str(config.device),
            },
        )
        operations.publish_daily_use(
            result,
            bundle,
            operations.transcript,
            transport,
            fixture.daily_use_evidence_summary(),
        )
        if interruption is not None:
            raise interruption
        return result
    finally:
        try:
            fixture.close()
        finally:
            operations.close()


def _create_output_directory(path: Path) -> PinnedOutputDirectory:
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
            raise HostGateError("physical daily-use output parent is unsafe")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as error:
        raise HostGateError("physical daily-use output already exists") from error
    output = PinnedOutputDirectory(path)
    output.lock_exclusive()
    return output


class RealDailyUsePhysicalOperations:
    """Compose the proven physical board lifecycle with daily-use publication."""

    def __init__(
        self,
        plan: DebugPlan,
        config: DailyUsePhysicalConfig,
        *,
        mmc_artifacts: Mapping[str, str] | None = None,
    ) -> None:
        self._output_path = config.output_directory
        self._output: PinnedOutputDirectory | None = None
        self._real = RealPhysicalGraphicsOperations(
            plan,
            str(config.device),
            config.output_directory,
            None,
            display_mode=DisplayEvidenceMode.OPERATOR_ATTESTED,
            cycles_requested=1,
            mmc_artifacts=mmc_artifacts,
        )

    @property
    def transcript(self) -> str:
        return self._real.transcript

    @property
    def guest_started(self) -> bool:
        return self._real.guest_started

    def invalidate(self) -> None:
        self._output = _create_output_directory(self._output_path)

    def open(self, timeout: float) -> None:
        self._real.open(timeout)

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]:
        return self._real.ensure_artifacts(plan, timeout)

    def boot(self, plan: DebugPlan, bootargs: str, timeout: float) -> None:
        self._real.boot(plan, bootargs, timeout)

    def prove_graphical_readiness(self, timeout: float) -> GraphicalReadinessEvidence:
        return self._real.prove_graphical_readiness(timeout)

    def run_daily_use_profile(
        self, experiment_id: str, timeout: float, expected_firefox_pid: int
    ) -> DailyUseTerminalStatus:
        return self._real.run_daily_use_profile(
            experiment_id, timeout, expected_firefox_pid
        )

    def request_reboot(self, timeout: float) -> None:
        self._real.request_reboot(timeout)

    def await_recovery(self, timeout: float) -> None:
        self._real.await_recovery(timeout)

    def publish_daily_use(
        self,
        result: DailyUsePhysicalResult,
        bundle: EvidenceBundle | None,
        transcript: str,
        transport: tuple[str, ...],
        fixture_summary: dict[str, object] | None,
    ) -> None:
        if self._output is None:
            raise HostGateError("physical daily-use output is not pinned")
        payloads: list[tuple[str, bytes]] = []
        if bundle is not None:
            payloads.extend(bundle.artifacts.items())
        payloads.extend(
            (
                ("serial.log", transcript.encode()),
                ("fixture-summary.json", _canonical_json(fixture_summary)),
                (
                    "deployment.json",
                    _canonical_json(
                        {
                            **result.deployment,
                            "transport": list(transport),
                        }
                    ),
                ),
                ("run-result.json", result.canonical_bytes()),
            )
        )
        manifest_rows = []
        for name, payload in payloads:
            self._output.atomic_write(name, payload, mode=0o600)
            manifest_rows.append(
                {
                    "name": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        self._output.atomic_write(
            "sha256-manifest.json",
            _canonical_json({"schemaVersion": 1, "files": manifest_rows}),
            mode=0o600,
        )

    def close(self) -> None:
        try:
            self._real.close()
        finally:
            if self._output is not None:
                self._output.close()
                self._output = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--fixture-bind", default=FIXTURE_BIND)
    parser.add_argument("--fixture-port", type=int, default=FIXTURE_PORT)
    parser.add_argument("--board-peer", default=BOARD_PEER)
    parser.add_argument("--profile-timeout", type=float, default=120.0)
    parser.add_argument("--open-timeout", type=float, default=60.0)
    parser.add_argument("--artifact-timeout", type=float, default=300.0)
    parser.add_argument("--boot-timeout", type=float, default=300.0)
    parser.add_argument("--upload-timeout", type=float, default=30.0)
    parser.add_argument("--recovery-timeout", type=float, default=930.0)
    parser.add_argument("--mmc-kernel")
    parser.add_argument("--mmc-initramfs")
    parser.add_argument("--mmc-dtb")
    parser.add_argument("--prepare-only", action="store_true")
    return parser


def _non_prepare_command(options: argparse.Namespace) -> str:
    arguments = [
        "python3",
        "-m",
        "tools.riscv.megrez_firefox_daily_use",
        str(options.device),
        "--plan",
        str(options.plan),
        "--output-directory",
        str(options.output_directory),
        "--fixture-bind",
        options.fixture_bind,
        "--fixture-port",
        str(options.fixture_port),
        "--board-peer",
        options.board_peer,
        "--profile-timeout",
        f"{options.profile_timeout:g}",
        "--open-timeout",
        f"{options.open_timeout:g}",
        "--artifact-timeout",
        f"{options.artifact_timeout:g}",
        "--boot-timeout",
        f"{options.boot_timeout:g}",
        "--upload-timeout",
        f"{options.upload_timeout:g}",
        "--recovery-timeout",
        f"{options.recovery_timeout:g}",
    ]
    for option, value in (
        ("--mmc-kernel", options.mmc_kernel),
        ("--mmc-initramfs", options.mmc_initramfs),
        ("--mmc-dtb", options.mmc_dtb),
    ):
        if value is not None:
            arguments.extend((option, value))
    return shlex.join(arguments)


def main(argv: Sequence[str] | None = None) -> int:
    """Run or prepare one closed physical daily-use command."""

    options = _parser().parse_args(argv)
    mmc_values = (options.mmc_kernel, options.mmc_initramfs, options.mmc_dtb)
    if any(value is not None for value in mmc_values) and not all(
        value is not None for value in mmc_values
    ):
        raise HostGateError("MMC kernel, initramfs, and DTB are all-or-none")
    config = DailyUsePhysicalConfig(
        device=options.device,
        output_directory=options.output_directory,
        fixture_bind=options.fixture_bind,
        fixture_port=options.fixture_port,
        board_peer=options.board_peer,
        profile_timeout=options.profile_timeout,
        open_timeout=options.open_timeout,
        artifact_timeout=options.artifact_timeout,
        boot_timeout=options.boot_timeout,
        upload_timeout=options.upload_timeout,
        recovery_timeout=options.recovery_timeout,
    )
    plan = _read_plan(options.plan)
    plan.validate()
    _validate_physical_artifacts(plan)
    daily_use_physical_bootargs(plan)
    if config.output_directory.exists() or config.output_directory.is_symlink():
        raise HostGateError("physical daily-use output already exists")
    if options.prepare_only:
        print(_non_prepare_command(options))
        return 0
    mmc_artifacts = None
    if all(value is not None for value in mmc_values):
        mmc_artifacts = {
            "kernel": options.mmc_kernel,
            "initramfs": options.mmc_initramfs,
            "megrez_dtb": options.mmc_dtb,
        }
    operations = RealDailyUsePhysicalOperations(
        plan, config, mmc_artifacts=mmc_artifacts
    )
    result = run_firefox_daily_use(plan, config, operations)
    return 0 if result.qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
