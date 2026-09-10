#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Start and diagnose the configured MMC-backed Megrez Debian desktop."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_board_session import safe_artifact_name
from tools.riscv.megrez_boot_stability import (
    BootReadinessEvidence,
    DeploymentAttestation,
    RealBootCycleOperations,
    boot_stability_bootargs,
    contains_fatal_diagnostics,
)
from tools.riscv.megrez_debug_contract import DebugPlan
from tools.riscv.megrez_physical_graphics import (
    HostGateError,
    _read_plan,
    physical_bootargs,
)


BUNDLE_SCHEMA_VERSION = 1
DEFAULT_BUNDLE = Path("target/megrez-desktop/current.json")
MMC_ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_ATTESTATION_BYTES = 2 * 1024 * 1024
MAX_MEASUREMENT_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024
MAX_TRANSPORT_RECORD_BYTES = 2048
MAX_MARIONETTE_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_FIREFOX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SERIAL_COMMAND_BYTES = 768
NEW_SESSION_HOST_GRACE_SECONDS = 15.0
FIREFOX_DIAGNOSTIC_PROTOCOL_VERSION = 5
MARIONETTE_TRANSPORT_PREFIX = "A_WEB_MARIONETTE_TRANSPORT "
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_BUNDLE_FIELDS = frozenset(
    {
        "schema_version",
        "device",
        "plan_path",
        "plan_sha256",
        "deployment_attestation_path",
        "deployment_attestation_sha256",
        "deployment_measurement_log_path",
        "deployment_measurement_log_sha256",
        "mmc_artifacts",
        "evidence_root",
    }
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{label} path is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    return path


def _read_bounded_regular(path: Path, label: str, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} is not a readable regular file: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file")
        if not 0 < before.st_size <= maximum:
            raise ValueError(f"{label} size is outside the bounded contract")
        chunks = bytearray()
        while len(chunks) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(chunks) != before.st_size
            or len(chunks) > maximum
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise ValueError(f"{label} changed while it was read")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} SHA-256 is invalid")
    return value


def _validate_device(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/dev/serial/by-id/")
        or "\0" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("serial device must be an absolute /dev/serial/by-id path")
    return value


def _validate_mmc_artifacts(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or set(value) != set(MMC_ARTIFACT_NAMES):
        raise ValueError("MMC artifact mapping must contain exactly three artifacts")
    validated: dict[str, str] = {}
    for name in MMC_ARTIFACT_NAMES:
        item = value[name]
        if not isinstance(item, str):
            raise ValueError(f"MMC artifact {name} must be text")
        try:
            validated[name] = safe_artifact_name(item)
        except (argparse.ArgumentTypeError, TypeError, ValueError) as error:
            raise ValueError(f"MMC artifact {name} is unsafe") from error
    return MappingProxyType(validated)


@dataclass(frozen=True)
class DesktopBundle:
    """One immutable, locally verified physical desktop deployment."""

    schema_version: int
    device: str
    plan_path: str
    plan_sha256: str
    deployment_attestation_path: str
    deployment_attestation_sha256: str
    deployment_measurement_log_path: str
    deployment_measurement_log_sha256: str
    mmc_artifacts: Mapping[str, str]
    evidence_root: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("desktop bundle schema version is invalid")
        _validate_device(self.device)
        for value, label in (
            (self.plan_path, "plan"),
            (self.deployment_attestation_path, "deployment attestation"),
            (self.deployment_measurement_log_path, "deployment measurement log"),
            (self.evidence_root, "evidence root"),
        ):
            _absolute_path(value, label)
        for value, label in (
            (self.plan_sha256, "plan"),
            (self.deployment_attestation_sha256, "deployment attestation"),
            (self.deployment_measurement_log_sha256, "deployment measurement log"),
        ):
            _validate_digest(value, label)
        object.__setattr__(
            self, "mmc_artifacts", _validate_mmc_artifacts(dict(self.mmc_artifacts))
        )

    def _json_value(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "device": self.device,
            "plan_path": self.plan_path,
            "plan_sha256": self.plan_sha256,
            "deployment_attestation_path": self.deployment_attestation_path,
            "deployment_attestation_sha256": self.deployment_attestation_sha256,
            "deployment_measurement_log_path": self.deployment_measurement_log_path,
            "deployment_measurement_log_sha256": (
                self.deployment_measurement_log_sha256
            ),
            "mmc_artifacts": dict(self.mmc_artifacts),
            "evidence_root": self.evidence_root,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self._json_value())

    @classmethod
    def from_path(cls, path: Path) -> DesktopBundle:
        payload = _read_bounded_regular(path, "desktop bundle", MAX_BUNDLE_BYTES)
        try:
            value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("desktop bundle JSON is invalid") from error
        if not isinstance(value, dict) or set(value) != _BUNDLE_FIELDS:
            raise ValueError("desktop bundle fields are invalid")
        bundle = cls(**value)
        if bundle.canonical_bytes() != payload:
            raise ValueError("desktop bundle JSON is not canonical")
        _validated_bound_plan(bundle)
        return bundle


def _validated_bound_plan(bundle: DesktopBundle) -> DebugPlan:
    """Read, hash, parse, and cross-check every bundle input exactly once."""

    payloads: dict[str, bytes] = {}
    for input_path, expected, label, maximum in (
        (bundle.plan_path, None, "plan", MAX_PLAN_BYTES),
        (
            bundle.deployment_attestation_path,
            bundle.deployment_attestation_sha256,
            "deployment attestation",
            MAX_ATTESTATION_BYTES,
        ),
        (
            bundle.deployment_measurement_log_path,
            bundle.deployment_measurement_log_sha256,
            "deployment measurement log",
            MAX_MEASUREMENT_BYTES,
        ),
    ):
        payload = _read_bounded_regular(Path(input_path), label, maximum)
        if expected is not None and _sha256(payload) != expected:
            raise ValueError(f"{label} changed after bundle configuration")
        payloads[label] = payload
    try:
        plan = DebugPlan.from_bytes(payloads["plan"])
    except ValueError as error:
        raise ValueError(f"desktop bundle plan is invalid: {error}") from error
    if plan.schema_version != 2 or plan.profile != "debian-browser":
        raise ValueError("desktop bundle requires a Debian browser plan")
    if plan.plan_sha256 != bundle.plan_sha256:
        raise ValueError("plan changed after bundle configuration")
    try:
        attestation_value = json.loads(
            payloads["deployment attestation"],
            object_pairs_hook=_reject_duplicate_keys,
        )
        attestation = DeploymentAttestation.from_mapping(attestation_value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("desktop bundle deployment attestation is invalid") from error
    attestation.validate(
        plan,
        bundle.mmc_artifacts,
        payloads["deployment measurement log"],
    )
    return plan


def _publish_bundle(destination: Path, payload: bytes) -> None:
    if not destination.is_absolute():
        raise ValueError("desktop bundle destination must be absolute")
    parent = destination.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("desktop bundle output directory is unsafe")
    if destination.exists() and (destination.is_symlink() or not destination.is_file()):
        raise ValueError("desktop bundle destination is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def configure_bundle(
    *,
    plan_path: Path,
    device: str,
    deployment_attestation_path: Path,
    deployment_measurement_log_path: Path,
    mmc_artifacts: Mapping[str, str],
    evidence_root: Path,
    destination: Path,
) -> DesktopBundle:
    """Validate and atomically configure one existing MMC deployment."""

    validated_device = _validate_device(device)
    paths = (
        (plan_path, "plan", MAX_PLAN_BYTES),
        (
            deployment_attestation_path,
            "deployment attestation",
            MAX_ATTESTATION_BYTES,
        ),
        (
            deployment_measurement_log_path,
            "deployment measurement log",
            MAX_MEASUREMENT_BYTES,
        ),
    )
    for path, label, _maximum in paths:
        if not path.is_absolute():
            raise ValueError(f"{label} path must be absolute")
    if not evidence_root.is_absolute():
        raise ValueError("evidence root path must be absolute")
    if evidence_root.exists() and evidence_root.is_symlink():
        raise ValueError("evidence root path is unsafe")
    validated_mmc = _validate_mmc_artifacts(dict(mmc_artifacts))
    payloads = {
        label: _read_bounded_regular(path, label, maximum)
        for path, label, maximum in paths
    }
    try:
        plan = DebugPlan.from_bytes(payloads["plan"])
    except ValueError as error:
        raise ValueError(f"desktop bundle plan is invalid: {error}") from error
    if plan.schema_version != 2 or plan.profile != "debian-browser":
        raise ValueError("desktop bundle requires a Debian browser plan")
    measurement_digest = _sha256(payloads["deployment measurement log"])
    bundle = DesktopBundle(
        schema_version=BUNDLE_SCHEMA_VERSION,
        device=validated_device,
        plan_path=str(plan_path),
        plan_sha256=plan.plan_sha256,
        deployment_attestation_path=str(deployment_attestation_path),
        deployment_attestation_sha256=_sha256(payloads["deployment attestation"]),
        deployment_measurement_log_path=str(deployment_measurement_log_path),
        deployment_measurement_log_sha256=measurement_digest,
        mmc_artifacts=validated_mmc,
        evidence_root=str(evidence_root),
    )
    _validated_bound_plan(bundle)
    _publish_bundle(destination, bundle.canonical_bytes())
    return bundle


@dataclass(frozen=True)
class DesktopStartConfig:
    """Independent bounded deadlines for one desktop startup attempt."""

    open_timeout: float = 60.0
    artifact_timeout: float = 300.0
    boot_timeout: float = 180.0
    readiness_timeout: float = 240.0
    diagnostics_timeout: float = 60.0
    reboot_timeout: float = 30.0
    recovery_timeout: float = 180.0

    def __post_init__(self) -> None:
        deadlines = (
            self.open_timeout,
            self.artifact_timeout,
            self.boot_timeout,
            self.readiness_timeout,
            self.diagnostics_timeout,
            self.reboot_timeout,
            self.recovery_timeout,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1200
            for value in deadlines
        ):
            raise ValueError("desktop-start deadlines must be in (0, 1200]")


@dataclass(frozen=True)
class DesktopStartResult:
    """Canonical terminal evidence for one configured desktop startup."""

    schema_version: int
    passed: bool
    physical: bool
    reason: str
    failure: str
    plan_sha256: str
    bootargs_sha256: str
    recovered: bool
    readiness: BootReadinessEvidence | None
    transport: tuple[str, ...]
    serial_sha256: str
    diagnostics_sha256: str
    artifact_seconds: float
    readiness_seconds: float
    diagnostics_seconds: float
    recovery_seconds: float
    total_seconds: float

    def __post_init__(self) -> None:
        durations = (
            self.artifact_seconds,
            self.readiness_seconds,
            self.diagnostics_seconds,
            self.recovery_seconds,
            self.total_seconds,
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not isinstance(self.passed, bool)
            or self.physical is not True
            or not isinstance(self.reason, str)
            or not self.reason
            or not isinstance(self.failure, str)
            or _SHA256.fullmatch(self.plan_sha256) is None
            or _SHA256.fullmatch(self.bootargs_sha256) is None
            or not isinstance(self.recovered, bool)
            or any(not isinstance(item, str) or not item for item in self.transport)
            or _SHA256.fullmatch(self.serial_sha256) is None
            or _SHA256.fullmatch(self.diagnostics_sha256) is None
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in durations
            )
        ):
            raise HostGateError("desktop-start result is invalid")
        if self.passed:
            if (
                self.reason != "desktop-ready"
                or self.failure
                or self.recovered
                or self.readiness is None
                or not self.transport
            ):
                raise HostGateError("passing desktop-start result is incomplete")
        elif self.reason == "desktop-ready":
            raise HostGateError("failed desktop-start result uses pass reason")

    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))


class DesktopOperations(Protocol):
    """Side effects required for one configured desktop start."""

    @property
    def guest_started(self) -> bool: ...

    @property
    def transcript(self) -> str | bytes: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: Any, timeout: float) -> tuple[str, ...]: ...

    def boot(self, plan: Any, bootargs: str, timeout: float) -> None: ...

    def prove_boot_readiness(self, timeout: float) -> BootReadinessEvidence: ...

    def collect_diagnostics(self, timeout: float) -> bytes: ...

    def request_reboot(self, timeout: float) -> None: ...

    def await_recovery(self, timeout: float) -> None: ...

    def close(self) -> None: ...


class DesktopPublisher(Protocol):
    """Atomic output operations for one desktop start attempt."""

    def invalidate(self) -> None: ...

    def publish(
        self, result: DesktopStartResult, serial: bytes, diagnostics: bytes
    ) -> None: ...


def desktop_start_bootargs(plan: Any) -> str:
    """Derive a local, read-only desktop boot without an automatic reboot."""

    excluded = (
        "asterinas.net=",
        "asterinas.neighbor=",
        "asterinas.reboot_after=",
        "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_",
    )
    return " ".join(
        token
        for token in physical_bootargs(plan).split()
        if not token.startswith(excluded)
    )


def _elapsed_seconds(clock: Callable[[], float], start: float) -> float:
    elapsed = clock() - start
    if not math.isfinite(elapsed) or elapsed < 0:
        raise HostGateError("monotonic clock moved backwards")
    return round(elapsed, 3)


def _transcript_bytes(transcript: str | bytes) -> bytes:
    if isinstance(transcript, str):
        return transcript.encode("utf-8")
    if isinstance(transcript, bytes):
        return transcript
    raise HostGateError("serial transcript must be text or bytes")


def _failure_reason(error: BaseException) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", type(error).__name__).lower()
    detail = re.sub(r"[^a-z0-9]+", "-", str(error).lower()).strip("-")[:96]
    return f"{name}-{detail or 'unspecified'}"


def _run_desktop_start(
    plan: Any,
    config: DesktopStartConfig,
    operations: DesktopOperations,
    publisher: DesktopPublisher,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> DesktopStartResult:
    """Start one desktop, leaving success running and recovering failures."""

    plan.validate()
    bootargs = desktop_start_bootargs(plan)
    publisher.invalidate()
    total_start = clock()
    readiness: BootReadinessEvidence | None = None
    transport: tuple[str, ...] = ()
    diagnostics = b""
    serial = b""
    recovered = False
    artifact_seconds = 0.0
    readiness_seconds = 0.0
    diagnostics_seconds = 0.0
    recovery_seconds = 0.0
    failure: BaseException | None = None
    interruption: BaseException | None = None
    failure_details: list[str] = []
    recovery_failed = False

    try:
        artifact_start = clock()
        operations.open(config.open_timeout)
        transport = operations.ensure_artifacts(plan, config.artifact_timeout)
        artifact_seconds = _elapsed_seconds(clock, artifact_start)

        readiness_start = clock()
        operations.boot(plan, bootargs, config.boot_timeout)
        readiness = operations.prove_boot_readiness(config.readiness_timeout)
        readiness_seconds = _elapsed_seconds(clock, readiness_start)
        serial = _transcript_bytes(operations.transcript)
        if contains_fatal_diagnostics(serial):
            raise HostGateError("fatal diagnostics in serial transcript")
    except Exception as error:
        failure = error
        failure_details.append(_failure_reason(error))
    except BaseException as error:
        interruption = error
        failure_details.append(_failure_reason(error))

    if operations.guest_started and (failure is not None or interruption is not None):
        diagnostics_start = clock()
        try:
            collected = operations.collect_diagnostics(config.diagnostics_timeout)
            if not isinstance(collected, bytes):
                raise HostGateError("desktop diagnostics must be bytes")
            diagnostics = collected
        except Exception as error:
            diagnostics = b""
            failure_details.append(f"diagnostics-{_failure_reason(error)}")
        except BaseException as error:
            diagnostics = b""
            if interruption is None:
                interruption = error
            failure_details.append(f"diagnostics-{_failure_reason(error)}")
        diagnostics_seconds = _elapsed_seconds(clock, diagnostics_start)

        recovery_start = clock()
        try:
            operations.request_reboot(config.reboot_timeout)
        except Exception as error:
            failure_details.append(f"reboot-{_failure_reason(error)}")
        except BaseException as error:
            if interruption is None:
                interruption = error
            failure_details.append(f"reboot-{_failure_reason(error)}")
        try:
            operations.await_recovery(config.recovery_timeout)
            recovered = True
        except Exception as error:
            recovery_failed = True
            failure_details.append(f"recovery-{_failure_reason(error)}")
        except BaseException as error:
            recovery_failed = True
            if interruption is None:
                interruption = error
            failure_details.append(f"recovery-{_failure_reason(error)}")
        recovery_seconds = _elapsed_seconds(clock, recovery_start)

    try:
        serial = _transcript_bytes(operations.transcript)
    except Exception as error:
        if failure is None:
            failure = error
        failure_details.append(f"transcript-{_failure_reason(error)}")

    passed = failure is None and interruption is None
    if passed:
        reason = "desktop-ready"
    elif recovery_failed:
        reason = "manual-reset-required"
    else:
        primary = failure if failure is not None else interruption
        assert primary is not None
        reason = f"desktop-start-{_failure_reason(primary)}"
    result = DesktopStartResult(
        schema_version=1,
        passed=passed,
        physical=True,
        reason=reason,
        failure=";".join(failure_details),
        plan_sha256=plan.plan_sha256,
        bootargs_sha256=_sha256(bootargs.encode()),
        recovered=recovered,
        readiness=readiness,
        transport=transport,
        serial_sha256=_sha256(serial),
        diagnostics_sha256=_sha256(diagnostics),
        artifact_seconds=artifact_seconds,
        readiness_seconds=readiness_seconds,
        diagnostics_seconds=diagnostics_seconds,
        recovery_seconds=recovery_seconds,
        total_seconds=_elapsed_seconds(clock, total_start),
    )
    publisher.publish(result, serial, diagnostics)
    if interruption is not None:
        raise interruption
    return result


def run_desktop_start(
    plan: Any,
    config: DesktopStartConfig,
    operations: DesktopOperations,
    publisher: DesktopPublisher,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> DesktopStartResult:
    """Close the host serial descriptor on every desktop-start path."""

    try:
        return _run_desktop_start(
            plan,
            config,
            operations,
            publisher,
            clock=clock,
        )
    finally:
        operations.close()


@dataclass(frozen=True)
class MarionetteTransportRecord:
    """One payload-free progress record emitted by the frozen guest client."""

    version: int
    event: str
    pid: int
    monotonic_ns: int
    request_id: int
    command: str
    stage: str
    send_complete: bool
    header_bytes: int
    body_expected: int | None
    body_received: int
    error_type: str | None = None
    errno: int | None = None


@dataclass(frozen=True)
class FirefoxBoundaryEvidence:
    """Earliest supported boundary in the selected NewSession transport."""

    boundary: str
    status_complete: bool
    new_session_request_id: int | None
    send_complete: bool
    response_header_bytes: int
    response_body_expected: int | None
    response_body_received: int
    selected_command_seconds: float | None

    def __post_init__(self) -> None:
        if (
            self.boundary
            not in {
                "listener-not-ready",
                "status-command-stalled",
                "new-session-not-sent",
                "new-session-response-absent",
                "new-session-response-partial",
                "new-session-complete",
                "evidence-incomplete",
            }
            or not isinstance(self.status_complete, bool)
            or (
                self.new_session_request_id is not None
                and (
                    type(self.new_session_request_id) is not int
                    or self.new_session_request_id <= 0
                )
            )
            or not isinstance(self.send_complete, bool)
            or type(self.response_header_bytes) is not int
            or not 0 <= self.response_header_bytes <= 11
            or (
                self.response_body_expected is not None
                and (
                    type(self.response_body_expected) is not int
                    or self.response_body_expected < 0
                )
            )
            or type(self.response_body_received) is not int
            or self.response_body_received < 0
            or (
                self.response_body_expected is not None
                and self.response_body_received > self.response_body_expected
            )
            or (
                self.selected_command_seconds is not None
                and (
                    isinstance(self.selected_command_seconds, bool)
                    or not isinstance(self.selected_command_seconds, (int, float))
                    or not math.isfinite(self.selected_command_seconds)
                    or self.selected_command_seconds < 0
                )
            )
        ):
            raise HostGateError("Firefox boundary evidence is invalid")


_TRANSPORT_BASE_FIELDS = frozenset(
    {
        "version",
        "event",
        "pid",
        "monotonic_ns",
        "request_id",
        "command",
        "stage",
        "send_complete",
        "header_bytes",
        "body_expected",
        "body_received",
    }
)
_TRANSPORT_FAILURE_FIELDS = _TRANSPORT_BASE_FIELDS | {"error_type", "errno"}
_TRANSPORT_COMMANDS = {
    "greeting",
    "WebDriver:Status",
    "WebDriver:NewSession",
}
_TRANSPORT_STAGES = {
    "tcp_connect",
    "response_header",
    "response_body",
    "response_json",
    "response_identity",
    "send",
    "complete",
}


def _parse_transport_record(value: object) -> MarionetteTransportRecord:
    if not isinstance(value, dict):
        raise HostGateError("Marionette transport record must be an object")
    event = value.get("event")
    if not isinstance(event, str) or event not in {
        "begin",
        "send_complete",
        "frame_header",
        "complete",
        "failure",
    }:
        raise HostGateError("Marionette transport record values are invalid")
    expected_fields = (
        _TRANSPORT_FAILURE_FIELDS if event == "failure" else _TRANSPORT_BASE_FIELDS
    )
    if set(value) != expected_fields:
        raise HostGateError("Marionette transport record fields are invalid")
    command = value["command"]
    if not isinstance(command, str) or command not in _TRANSPORT_COMMANDS:
        raise HostGateError("Marionette transport record has wrong command")
    stage = value["stage"]
    if not isinstance(stage, str) or stage not in _TRANSPORT_STAGES:
        raise HostGateError("Marionette transport record values are invalid")
    body_expected = value["body_expected"]
    errno_value = value.get("errno")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or type(value["pid"]) is not int
        or not 1 < value["pid"] <= (1 << 31) - 1
        or type(value["monotonic_ns"]) is not int
        or not 0 <= value["monotonic_ns"] <= (1 << 63) - 1
        or type(value["request_id"]) is not int
        or not 0 <= value["request_id"] <= (1 << 31) - 1
        or not isinstance(value["send_complete"], bool)
        or type(value["header_bytes"]) is not int
        or not 0 <= value["header_bytes"] <= 11
        or (
            body_expected is not None
            and (
                type(body_expected) is not int
                or not 0 <= body_expected <= MAX_MARIONETTE_MESSAGE_BYTES
            )
        )
        or type(value["body_received"]) is not int
        or value["body_received"] < 0
        or (body_expected is not None and value["body_received"] > body_expected)
        or (
            event == "failure"
            and (
                not isinstance(value["error_type"], str)
                or not value["error_type"]
                or len(value["error_type"]) > 128
                or (
                    errno_value is not None
                    and (type(errno_value) is not int or errno_value < 0)
                )
            )
        )
    ):
        raise HostGateError("Marionette transport record values are invalid")
    request_id = value["request_id"]
    if (command == "greeting") != (request_id == 0):
        raise HostGateError("Marionette transport request order is invalid")

    send_complete = value["send_complete"]
    header_bytes = value["header_bytes"]
    body_received = value["body_received"]
    if event == "begin" and not (
        command != "greeting"
        and stage == "send"
        and not send_complete
        and header_bytes == 0
        and body_expected is None
        and body_received == 0
    ):
        raise HostGateError("Marionette begin progress is contradictory")
    if event == "send_complete" and not (
        command != "greeting"
        and stage == "send"
        and send_complete
        and header_bytes == 0
        and body_expected is None
        and body_received == 0
    ):
        raise HostGateError("Marionette send progress is contradictory")
    if event == "frame_header" and not (
        stage == "response_body"
        and send_complete == (command != "greeting")
        and header_bytes >= 2
        and body_expected is not None
        and body_received == 0
    ):
        raise HostGateError("Marionette header progress is contradictory")
    if event == "complete" and not (
        command != "greeting"
        and stage == "complete"
        and send_complete
        and header_bytes >= 2
        and body_expected is not None
        and body_received == body_expected
    ):
        raise HostGateError("Marionette completion progress is contradictory")
    if event == "failure":
        valid_failure_progress = (
            (
                stage == "tcp_connect"
                and command == "greeting"
                and not send_complete
                and header_bytes == 0
                and body_expected is None
                and body_received == 0
            )
            or (
                stage == "send"
                and command != "greeting"
                and not send_complete
                and header_bytes == 0
                and body_expected is None
                and body_received == 0
            )
            or (
                stage == "response_header"
                and send_complete == (command != "greeting")
                and body_expected is None
                and body_received == 0
            )
            or (
                stage == "response_body"
                and send_complete == (command != "greeting")
                and header_bytes >= 2
                and body_expected is not None
            )
            or (
                stage == "response_json"
                and send_complete == (command != "greeting")
                and header_bytes >= 2
                and body_expected is not None
                and body_received == body_expected
            )
            or (
                stage == "response_identity"
                and command != "greeting"
                and send_complete
                and header_bytes >= 2
                and body_expected is not None
                and body_received == body_expected
            )
        )
        if not valid_failure_progress:
            raise HostGateError("Marionette failure progress is contradictory")
    return MarionetteTransportRecord(
        version=value["version"],
        event=event,
        pid=value["pid"],
        monotonic_ns=value["monotonic_ns"],
        request_id=request_id,
        command=command,
        stage=stage,
        send_complete=send_complete,
        header_bytes=header_bytes,
        body_expected=body_expected,
        body_received=body_received,
        error_type=value.get("error_type"),
        errno=errno_value,
    )


def _validate_transport_sequence(
    records: tuple[MarionetteTransportRecord, ...],
) -> None:
    streams: dict[tuple[int, int, str], list[MarionetteTransportRecord]] = {}
    request_commands: dict[tuple[int, int], str] = {}
    first_positions: dict[tuple[int, int, str], int] = {}
    last_positions: dict[tuple[int, int, str], int] = {}
    terminal: set[tuple[int, int, str]] = set()

    for position, record in enumerate(records):
        request_key = (record.pid, record.request_id)
        previous_command = request_commands.setdefault(request_key, record.command)
        if previous_command != record.command:
            raise HostGateError("Marionette transport request order is invalid")
        key = (record.pid, record.request_id, record.command)
        first_positions.setdefault(key, position)
        stream = streams.setdefault(key, [])
        if key in terminal:
            raise HostGateError("Marionette transport has duplicate terminal record")
        if stream:
            previous = stream[-1]
            if (
                record.monotonic_ns < previous.monotonic_ns
                or (previous.send_complete and not record.send_complete)
                or record.header_bytes < previous.header_bytes
                or record.body_received < previous.body_received
                or (
                    previous.body_expected is not None
                    and record.body_expected != previous.body_expected
                )
            ):
                raise HostGateError("Marionette transport progress is reordered")
        if record.command == "greeting":
            if not stream and record.event not in {"frame_header", "failure"}:
                raise HostGateError("Marionette greeting record order is invalid")
            if stream and record.event != "failure":
                raise HostGateError("Marionette greeting record order is invalid")
        else:
            events = {item.event for item in stream}
            if not stream and record.event != "begin":
                raise HostGateError("Marionette command record order is invalid")
            if record.event in events:
                raise HostGateError("Marionette command record order is invalid")
            if record.event == "begin" and stream:
                raise HostGateError("Marionette command record order is invalid")
            if record.event == "send_complete" and "begin" not in events:
                raise HostGateError("Marionette command record order is invalid")
            if record.event == "frame_header" and not {
                "begin",
                "send_complete",
            }.issubset(events):
                raise HostGateError("Marionette command record order is invalid")
            if record.event == "complete" and not {
                "begin",
                "send_complete",
                "frame_header",
            }.issubset(events):
                raise HostGateError("Marionette command record order is invalid")
            if record.event == "failure":
                required = {"begin"}
                if record.stage != "send":
                    required.add("send_complete")
                if record.stage in {
                    "response_body",
                    "response_json",
                    "response_identity",
                }:
                    required.add("frame_header")
                if not required.issubset(events):
                    raise HostGateError("Marionette command record order is invalid")
        stream.append(record)
        last_positions[key] = position
        if record.event in {"complete", "failure"}:
            terminal.add(key)

    command_keys: dict[str, list[tuple[int, int, str]]] = {
        "WebDriver:Status": [],
        "WebDriver:NewSession": [],
    }
    for key in streams:
        if key[2] in command_keys:
            command_keys[key[2]].append(key)
    if any(len(keys) > 1 for keys in command_keys.values()):
        raise HostGateError("Marionette transport request order is invalid")
    status_keys = command_keys["WebDriver:Status"]
    new_session_keys = command_keys["WebDriver:NewSession"]
    if new_session_keys and not status_keys:
        raise HostGateError("Marionette transport request order is invalid")
    if status_keys and new_session_keys:
        status_key = status_keys[0]
        new_session_key = new_session_keys[0]
        if (
            streams[status_key][-1].event != "complete"
            or first_positions[new_session_key] <= last_positions[status_key]
        ):
            raise HostGateError("Marionette transport request order is invalid")
    for keys in command_keys.values():
        for key in keys:
            if key[1] != 1:
                raise HostGateError("Marionette transport request order is invalid")
            greeting_key = (key[0], 0, "greeting")
            greeting = streams.get(greeting_key)
            if (
                not greeting
                or first_positions[greeting_key] >= first_positions[key]
                or greeting[-1].event == "failure"
            ):
                raise HostGateError("Marionette transport request order is invalid")


def parse_marionette_transport_records(
    transcript: str | bytes,
) -> tuple[MarionetteTransportRecord, ...]:
    """Parse strict, bounded payload-free records from an arbitrary serial log."""

    if isinstance(transcript, bytes):
        try:
            text = transcript.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HostGateError(
                "Marionette transport transcript is not UTF-8"
            ) from error
    elif isinstance(transcript, str):
        text = transcript
    else:
        raise HostGateError("Marionette transport transcript must be text or bytes")
    records: list[MarionetteTransportRecord] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if not stripped.startswith(MARIONETTE_TRANSPORT_PREFIX):
            continue
        if not line.endswith("\n"):
            raise HostGateError("Marionette transport record is truncated")
        encoded = stripped.removeprefix(MARIONETTE_TRANSPORT_PREFIX).encode("utf-8")
        if len(encoded) > MAX_TRANSPORT_RECORD_BYTES:
            raise HostGateError("Marionette transport record is oversized")
        try:
            value = json.loads(encoded, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise HostGateError(
                "Marionette transport record has malformed JSON"
            ) from error
        records.append(_parse_transport_record(value))
    parsed = tuple(records)
    _validate_transport_sequence(parsed)
    return parsed


def _boundary_evidence(
    boundary: str,
    *,
    status_complete: bool = False,
    records: tuple[MarionetteTransportRecord, ...] = (),
) -> FirefoxBoundaryEvidence:
    if not records:
        return FirefoxBoundaryEvidence(
            boundary=boundary,
            status_complete=status_complete,
            new_session_request_id=None,
            send_complete=False,
            response_header_bytes=0,
            response_body_expected=None,
            response_body_received=0,
            selected_command_seconds=None,
        )
    first = records[0]
    last = records[-1]
    expected = next(
        (
            record.body_expected
            for record in reversed(records)
            if record.body_expected is not None
        ),
        None,
    )
    return FirefoxBoundaryEvidence(
        boundary=boundary,
        status_complete=status_complete,
        new_session_request_id=first.request_id,
        send_complete=any(record.send_complete for record in records),
        response_header_bytes=max(record.header_bytes for record in records),
        response_body_expected=expected,
        response_body_received=max(record.body_received for record in records),
        selected_command_seconds=(last.monotonic_ns - first.monotonic_ns) / 1e9,
    )


def classify_new_session_transcript(
    transcript: str | bytes,
) -> FirefoxBoundaryEvidence:
    """Classify only the first transport boundary supported by retained records."""

    records = parse_marionette_transport_records(transcript)
    if not records:
        return _boundary_evidence("evidence-incomplete")
    status = tuple(record for record in records if record.command == "WebDriver:Status")
    if not status:
        return _boundary_evidence("listener-not-ready")
    status_complete = status[-1].event == "complete"
    if not status_complete:
        return _boundary_evidence("status-command-stalled")
    selected = tuple(
        record for record in records if record.command == "WebDriver:NewSession"
    )
    if not selected:
        return _boundary_evidence("new-session-not-sent", status_complete=True)
    if selected[-1].event == "complete":
        boundary = "new-session-complete"
    elif not any(record.send_complete for record in selected):
        boundary = "new-session-not-sent"
    elif max(record.header_bytes for record in selected) == 0:
        boundary = "new-session-response-absent"
    else:
        boundary = "new-session-response-partial"
    return _boundary_evidence(
        boundary,
        status_complete=True,
        records=selected,
    )


_SNAPSHOT_PHASES = ("before", "during", "after")
_SNAPSHOT_NONCE = re.compile(r"\A[0-9a-f]{16}\Z")
_SNAPSHOT_BEGIN = re.compile(
    r"\A__ASTERINAS_FIREFOX_SNAPSHOT_BEGIN__ "
    r"phase=(before|during|after) nonce=([0-9a-f]{16}) "
    r"size=([0-9]+) sha256=([0-9a-f]{64})\Z"
)
_SNAPSHOT_END = re.compile(
    r"\A__ASTERINAS_FIREFOX_SNAPSHOT_END__ "
    r"phase=(before|during|after) nonce=([0-9a-f]{16}) status=([0-9]+)\Z"
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "version",
        "physical",
        "root_pid",
        "root_identity",
        "duration_seconds",
        "bytes_read",
        "complete",
        "limitations",
        "processes",
    }
)


def _snapshot_frame_command(phase: str, nonce: str) -> str:
    zeros = "0" * 64
    return (
        f'_f="$_asterinas_firefox_base.{phase}"; _z=0; '
        '[ -f "$_f" ] && _z=$(wc -c <"$_f"); _s=1; '
        f'case "$_z" in ""|*[!0-9]*) _z=0;; esac; [ "$_z" -le {MAX_FIREFOX_SNAPSHOT_BYTES} ] '
        '&& [ -f "$_f" ] && _s=0; '
        f'if [ "$_s" -eq 0 ]; then _h=$(sha256sum "$_f" | cut -d\' \' -f1); '
        f"else _h={zeros}; fi; printf '__ASTERINAS_FIREFOX_SNAPSHOT_BEGIN__ "
        f'phase={phase} nonce={nonce} size=%s sha256=%s\\n\' "$_z" "$_h"; '
        'if [ "$_s" -eq 0 ]; then base64 -w 0 "$_f"; printf \'\\n\'; fi; '
        f"printf '__ASTERINAS_FIREFOX_SNAPSHOT_END__ phase={phase} nonce={nonce} "
        'status=%s\\n\' "$_s"; rm -f -- "$_f"'
    )


def firefox_diagnostic_commands(
    browser_pid: int,
    snapshot_nonces: Mapping[str, str],
    *,
    selected_timeout: float = 300.0,
) -> tuple[str, ...]:
    """Return short acknowledged commands for one payload-free diagnosis."""

    if type(browser_pid) is not int or not 1 < browser_pid <= (1 << 31) - 1:
        raise ValueError("Firefox PID is outside the valid contract")
    if not isinstance(snapshot_nonces, Mapping) or set(snapshot_nonces) != set(
        _SNAPSHOT_PHASES
    ):
        raise ValueError("Firefox snapshot nonces must cover exactly three phases")
    nonces = tuple(snapshot_nonces[phase] for phase in _SNAPSHOT_PHASES)
    if any(
        not isinstance(nonce, str) or _SNAPSHOT_NONCE.fullmatch(nonce) is None
        for nonce in nonces
    ) or len(set(nonces)) != len(nonces):
        raise ValueError("Firefox snapshot nonces must be unique lowercase hex")
    if (
        isinstance(selected_timeout, bool)
        or not isinstance(selected_timeout, (int, float))
        or not math.isfinite(selected_timeout)
        or not 0 < selected_timeout <= 300
    ):
        raise ValueError("Firefox selected-command timeout must be in (0, 300]")
    timeout = f"{selected_timeout:g}"
    run_nonce = nonces[0]
    snapshot_tool = (
        "/usr/bin/timeout 5 /usr/lib/asterinas/firefox-diagnostic-snapshot "
        '--root-pid "$_asterinas_firefox_pid" --max-seconds 3 '
        "--max-processes 16 --max-threads 128 --max-fds 64 --max-scan 1024 "
        "--max-file-bytes 8192 --max-total-bytes 262144"
    )
    environment = (
        "env -i PATH=/usr/bin:/bin PYTHONPATH=/usr/lib/asterinas "
        "ASTERINAS_MARIONETTE_DIAGNOSTICS=1"
    )
    commands = (
        f"_asterinas_firefox_pid={browser_pid}; "
        f"_asterinas_firefox_base=/run/asterinas-firefox-diagnostic-{run_nonce}; "
        'rm -f -- "$_asterinas_firefox_base".*; '
        f"_asterinas_firefox_snapshot() {{ {snapshot_tool}; }}",
        "_asterinas_firefox_current=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null); "
        "_asterinas_firefox_restarts=$(systemctl show --property NRestarts --value "
        "asterinas-browser-web.service 2>/dev/null)",
        "_asterinas_firefox_start=$(cut -d' ' -f22 "
        '"/proc/$_asterinas_firefox_pid/stat" 2>/dev/null); '
        "_asterinas_firefox_profile=$(stat -Lc '%d:%i' "
        "/home/asterinas/.mozilla/asterinas-browser-web 2>/dev/null)",
        'if [ "$_asterinas_firefox_current" = "$_asterinas_firefox_pid" ] && '
        '[ "$_asterinas_firefox_restarts" = 0 ] && '
        '[ -n "$_asterinas_firefox_start" ] && '
        '[ -n "$_asterinas_firefox_profile" ]; then '
        "printf '__ASTERINAS_FIREFOX_PREFLIGHT__ pid=%s start=%s restarts=%s "
        'profile=%s\\n\' "$_asterinas_firefox_pid" '
        '"$_asterinas_firefox_start" "$_asterinas_firefox_restarts" '
        '"$_asterinas_firefox_profile"; else false; fi',
        f'/usr/bin/nsenter -t "$_asterinas_firefox_pid" -n {environment} '
        "python3 -c 'from browser_m5_marionette_gate import status_once;"
        'status_once("127.0.0.1",2828,30)\'; '
        "_asterinas_firefox_status=$?; printf '__ASTERINAS_FIREFOX_STATUS__ "
        'status=%s\\n\' "$_asterinas_firefox_status"; :',
        '_asterinas_firefox_snapshot >"$_asterinas_firefox_base.before"; '
        "_asterinas_firefox_before_status=$?; "
        "printf '__ASTERINAS_FIREFOX_SNAPSHOT_STATUS__ phase=before status=%s\\n' "
        '"$_asterinas_firefox_before_status"; :',
        _snapshot_frame_command("before", snapshot_nonces["before"]),
        "( /usr/bin/sleep 5; _asterinas_firefox_snapshot "
        '>"$_asterinas_firefox_base.during"; printf \'%s\\n\' "$?" '
        '>"$_asterinas_firefox_base.during.status" ) & '
        "_asterinas_firefox_during_job=$!; :",
        f'/usr/bin/nsenter -t "$_asterinas_firefox_pid" -n {environment} '
        "python3 -c 'import time;from browser_m5_marionette_gate import _connect;"
        f'c=_connect("127.0.0.1",2828,time.monotonic()+{timeout});'
        'r=c.command("WebDriver:NewSession",'
        '{"pageLoadStrategy":"none","strictFileInteractability":True});'
        'c.close();assert isinstance(r,dict) and isinstance(r.get("sessionId"),str)\'; '
        "_asterinas_firefox_new_status=$?; printf '__ASTERINAS_FIREFOX_NEW_SESSION__ "
        'status=%s\\n\' "$_asterinas_firefox_new_status"; :',
        'wait "$_asterinas_firefox_during_job"; '
        "_asterinas_firefox_during_status=$(cat "
        '"$_asterinas_firefox_base.during.status" 2>/dev/null || printf 125); '
        "printf '__ASTERINAS_FIREFOX_SNAPSHOT_STATUS__ phase=during status=%s\\n' "
        '"$_asterinas_firefox_during_status"; :',
        _snapshot_frame_command("during", snapshot_nonces["during"]),
        '_asterinas_firefox_snapshot >"$_asterinas_firefox_base.after"; '
        "_asterinas_firefox_after_status=$?; "
        "printf '__ASTERINAS_FIREFOX_SNAPSHOT_STATUS__ phase=after status=%s\\n' "
        '"$_asterinas_firefox_after_status"; :',
        "_asterinas_firefox_terminal=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null); "
        "_asterinas_firefox_terminal_restarts=$(systemctl show --property NRestarts "
        "--value asterinas-browser-web.service 2>/dev/null)",
        "_asterinas_firefox_terminal_start=$(cut -d' ' -f22 "
        '"/proc/$_asterinas_firefox_pid/stat" 2>/dev/null); '
        "_asterinas_firefox_terminal_profile=$(stat -Lc '%d:%i' "
        "/home/asterinas/.mozilla/asterinas-browser-web 2>/dev/null)",
        'if [ "$_asterinas_firefox_terminal" = "$_asterinas_firefox_pid" ] && '
        '[ "$_asterinas_firefox_terminal_restarts" = 0 ] && '
        '[ "$_asterinas_firefox_terminal_start" = "$_asterinas_firefox_start" ] && '
        '[ "$_asterinas_firefox_terminal_profile" = "$_asterinas_firefox_profile" ]; '
        "then printf '__ASTERINAS_FIREFOX_TERMINAL__ pid=%s start=%s restarts=%s "
        "profile=%s\\n' "
        '"$_asterinas_firefox_pid" "$_asterinas_firefox_terminal_start" '
        '"$_asterinas_firefox_terminal_restarts" '
        '"$_asterinas_firefox_terminal_profile"; else false; fi',
        _snapshot_frame_command("after", snapshot_nonces["after"]),
        'rm -f -- "$_asterinas_firefox_base.during.status"; '
        "unset -f _asterinas_firefox_snapshot; :",
    )
    oversized = [
        index
        for index, command in enumerate(commands, start=1)
        if len((command + "\n").encode()) > MAX_SERIAL_COMMAND_BYTES
    ]
    if oversized:
        raise HostGateError(
            f"Firefox serial commands exceed the safe size: {oversized}"
        )
    return commands


def parse_firefox_snapshot_frame(
    transcript: str | bytes,
    phase: str,
    nonce: str,
    *,
    expected_root_pid: int,
) -> dict[str, object]:
    """Verify one nonce-bound snapshot frame before parsing its JSON payload."""

    if phase not in _SNAPSHOT_PHASES:
        raise ValueError("Firefox snapshot phase is invalid")
    if not isinstance(nonce, str) or _SNAPSHOT_NONCE.fullmatch(nonce) is None:
        raise ValueError("Firefox snapshot nonce is invalid")
    if type(expected_root_pid) is not int or not 1 < expected_root_pid <= (1 << 31) - 1:
        raise ValueError("Firefox snapshot root PID is invalid")
    try:
        text = _transcript_bytes(transcript).decode("utf-8")
    except UnicodeDecodeError as error:
        raise HostGateError("Firefox snapshot frame is not UTF-8") from error
    lines = tuple(line.rstrip("\r") for line in text.split("\n"))
    begins = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _SNAPSHOT_BEGIN.fullmatch(line)) is not None
        and match.group(1) == phase
        and match.group(2) == nonce
    ]
    ends = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _SNAPSHOT_END.fullmatch(line)) is not None
        and match.group(1) == phase
        and match.group(2) == nonce
    ]
    if len(begins) != 1 or len(ends) != 1:
        raise HostGateError("Firefox snapshot frame is missing or duplicated")
    begin_index, begin = begins[0]
    end_index, end = ends[0]
    if end_index != begin_index + 2 or end.group(3) != "0":
        raise HostGateError("Firefox snapshot frame status or ordering is invalid")
    expected_size = int(begin.group(3))
    if not 0 < expected_size <= MAX_FIREFOX_SNAPSHOT_BYTES:
        raise HostGateError("Firefox snapshot frame size is outside the contract")
    try:
        payload = base64.b64decode(lines[begin_index + 1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise HostGateError("Firefox snapshot frame is not canonical base64") from error
    if len(payload) != expected_size:
        raise HostGateError("Firefox snapshot frame size identity mismatch")
    if _sha256(payload) != begin.group(4):
        raise HostGateError("Firefox snapshot frame identity mismatch")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise HostGateError("Firefox snapshot frame JSON is invalid") from error
    except (UnicodeDecodeError, RecursionError) as error:
        raise HostGateError("Firefox snapshot frame JSON is invalid") from error
    except ValueError as error:
        if "duplicate JSON key" in str(error):
            raise HostGateError(
                "Firefox snapshot frame has duplicate JSON key"
            ) from error
        raise HostGateError("Firefox snapshot frame JSON is invalid") from error
    return _validate_firefox_snapshot(value, expected_root_pid)


def _validate_firefox_snapshot(
    value: object, expected_root_pid: int
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_FIELDS:
        raise HostGateError("Firefox snapshot frame JSON schema is invalid")
    root_identity = value["root_identity"]
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["physical"] is not False
        or type(value["root_pid"]) is not int
        or value["root_pid"] != expected_root_pid
        or (
            root_identity is not None
            and (
                not isinstance(root_identity, dict)
                or set(root_identity) != {"pid", "ppid", "start_time_ticks"}
                or any(
                    type(item) is not int or item < 0 for item in root_identity.values()
                )
                or root_identity["pid"] != expected_root_pid
            )
        )
        or isinstance(value["duration_seconds"], bool)
        or not isinstance(value["duration_seconds"], (int, float))
        or not math.isfinite(value["duration_seconds"])
        or not 0 <= value["duration_seconds"] <= 30
        or type(value["bytes_read"]) is not int
        or not 0 <= value["bytes_read"] <= 262144
        or not isinstance(value["complete"], bool)
        or not isinstance(value["limitations"], list)
        or not all(isinstance(item, str) and item for item in value["limitations"])
        or not isinstance(value["processes"], list)
        or len(value["processes"]) > 16
    ):
        raise HostGateError("Firefox snapshot frame JSON values are invalid")
    return value


@dataclass(frozen=True)
class FirefoxDiagnosticConfig:
    """One falsifiable Firefox experiment with bounded independent phases."""

    hypothesis: str
    contrary_outcome: str
    selected_command_timeout: float = 300.0
    total_timeout: float = 900.0
    open_timeout: float = 60.0
    artifact_timeout: float = 300.0
    boot_timeout: float = 180.0
    readiness_timeout: float = 240.0
    firefox_preflight_timeout: float = 30.0
    status_timeout: float = 60.0
    snapshot_timeout: float = 60.0
    diagnostics_timeout: float = 60.0
    reboot_timeout: float = 30.0
    recovery_timeout: float = 180.0

    def __post_init__(self) -> None:
        for value, label in (
            (self.hypothesis, "hypothesis"),
            (self.contrary_outcome, "contrary outcome"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.encode("utf-8")) > 512
                or any(character in value for character in ("\0", "\n", "\r"))
            ):
                raise ValueError(f"Firefox diagnostic {label} is invalid")
        deadlines = (
            self.selected_command_timeout,
            self.total_timeout,
            self.open_timeout,
            self.artifact_timeout,
            self.boot_timeout,
            self.readiness_timeout,
            self.firefox_preflight_timeout,
            self.status_timeout,
            self.snapshot_timeout,
            self.diagnostics_timeout,
            self.reboot_timeout,
            self.recovery_timeout,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1200
            for value in deadlines
        ):
            raise ValueError("Firefox diagnostic deadlines must be in (0, 1200]")
        if self.selected_command_timeout > 300 or self.total_timeout > 900:
            raise ValueError("Firefox diagnostic cost budget is exceeded")


@dataclass(frozen=True)
class FirefoxDiagnosticInputs:
    """Immutable hashes admitted before a physical experiment starts."""

    experiment_sha256: str
    bundle_sha256: str
    plan_sha256: str
    deployment_attestation_sha256: str
    deployment_measurement_log_sha256: str

    def __post_init__(self) -> None:
        if any(
            _SHA256.fullmatch(value) is None
            for value in (
                self.experiment_sha256,
                self.bundle_sha256,
                self.plan_sha256,
                self.deployment_attestation_sha256,
                self.deployment_measurement_log_sha256,
            )
        ):
            raise ValueError("Firefox diagnostic input hash is invalid")


@dataclass(frozen=True)
class FirefoxProcessIdentity:
    """Stable Firefox service and profile identity across one diagnosis."""

    pid: int
    start_time_ticks: int
    profile_identity: str
    browser_restarts: int

    def __post_init__(self) -> None:
        if (
            type(self.pid) is not int
            or not 1 < self.pid <= (1 << 31) - 1
            or type(self.start_time_ticks) is not int
            or self.start_time_ticks <= 0
            or not isinstance(self.profile_identity, str)
            or re.fullmatch(r"[0-9]+:[0-9]+", self.profile_identity) is None
            or type(self.browser_restarts) is not int
            or self.browser_restarts != 0
        ):
            raise HostGateError("Firefox process identity is invalid")


@dataclass(frozen=True)
class FirefoxDiagnosticResult:
    """Canonical evidence and cost accounting for one admitted experiment."""

    schema_version: int
    passed: bool
    physical: bool
    reason: str
    failure: str
    experiment_sha256: str
    bundle_sha256: str
    plan_sha256: str
    deployment_attestation_sha256: str
    deployment_measurement_log_sha256: str
    bootargs_sha256: str
    hypothesis: str
    contrary_outcome: str
    artifact_transfer_bytes: int
    qemu_runs: int
    physical_boots: int
    total_host_seconds: float
    boundary: FirefoxBoundaryEvidence
    snapshot_sha256: tuple[str, str, str]
    diagnostics_sha256: str
    serial_sha256: str
    firefox: FirefoxProcessIdentity | None
    transport: tuple[str, ...]
    recovered: bool

    def __post_init__(self) -> None:
        digests = (
            self.experiment_sha256,
            self.bundle_sha256,
            self.plan_sha256,
            self.deployment_attestation_sha256,
            self.deployment_measurement_log_sha256,
            self.bootargs_sha256,
            *self.snapshot_sha256,
            self.diagnostics_sha256,
            self.serial_sha256,
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not isinstance(self.passed, bool)
            or self.physical is not True
            or not isinstance(self.reason, str)
            or not self.reason
            or not isinstance(self.failure, str)
            or any(_SHA256.fullmatch(value) is None for value in digests)
            or not isinstance(self.hypothesis, str)
            or not isinstance(self.contrary_outcome, str)
            or type(self.artifact_transfer_bytes) is not int
            or self.artifact_transfer_bytes < 0
            or type(self.qemu_runs) is not int
            or self.qemu_runs != 0
            or type(self.physical_boots) is not int
            or self.physical_boots not in (0, 1)
            or isinstance(self.total_host_seconds, bool)
            or not isinstance(self.total_host_seconds, (int, float))
            or not math.isfinite(self.total_host_seconds)
            or self.total_host_seconds < 0
            or len(self.snapshot_sha256) != 3
            or any(not isinstance(item, str) or not item for item in self.transport)
            or not isinstance(self.recovered, bool)
        ):
            raise HostGateError("Firefox diagnostic result is invalid")
        if self.passed and (
            self.reason != "firefox-diagnosis-complete"
            or self.failure
            or self.artifact_transfer_bytes != 0
            or self.physical_boots != 1
            or self.boundary.boundary == "evidence-incomplete"
            or self.firefox is None
            or not self.transport
            or not self.recovered
        ):
            raise HostGateError("passing Firefox diagnostic result is incomplete")
        if not self.passed and self.reason == "firefox-diagnosis-complete":
            raise HostGateError("failed Firefox diagnostic result uses pass reason")

    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))


class FirefoxDiagnosticOperations(Protocol):
    """All bounded board and guest operations for one Firefox diagnosis."""

    @property
    def guest_started(self) -> bool: ...

    @property
    def transcript(self) -> str | bytes: ...

    @property
    def artifact_transfer_bytes(self) -> int: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: Any, timeout: float) -> tuple[str, ...]: ...

    def boot(self, plan: Any, bootargs: str, timeout: float) -> None: ...

    def prove_boot_readiness(self, timeout: float) -> BootReadinessEvidence: ...

    def firefox_preflight(
        self,
        browser_pid: int,
        snapshot_nonces: Mapping[str, str],
        selected_timeout: float,
        timeout: float,
    ) -> FirefoxProcessIdentity: ...

    def run_firefox_status(self, timeout: float) -> None: ...

    def capture_firefox_snapshot(
        self, phase: str, nonce: str, timeout: float
    ) -> dict[str, object]: ...

    def run_firefox_new_session(self, timeout: float) -> None: ...

    def collect_diagnostics(self, timeout: float) -> bytes: ...

    def request_reboot(self, timeout: float) -> None: ...

    def await_recovery(self, timeout: float) -> None: ...

    def close(self) -> None: ...


class FirefoxDiagnosticPublisher(Protocol):
    """Admission and atomic publication for one Firefox experiment."""

    @property
    def inputs(self) -> FirefoxDiagnosticInputs: ...

    def invalidate(self) -> None: ...

    def publish(
        self,
        result: FirefoxDiagnosticResult,
        serial: bytes,
        diagnostics: bytes,
        snapshots: Mapping[str, bytes],
    ) -> None: ...


def experiment_identity(
    bundle: DesktopBundle, plan: Any, config: FirefoxDiagnosticConfig
) -> str:
    """Identify runtime semantics while excluding human hypothesis wording."""

    plan.validate()
    if bundle.plan_sha256 != plan.plan_sha256:
        raise HostGateError("desktop bundle and Firefox plan identity differ")
    value = {
        "schema_version": 1,
        "diagnostic_protocol_version": FIREFOX_DIAGNOSTIC_PROTOCOL_VERSION,
        "plan_sha256": plan.plan_sha256,
        "deployment_attestation_sha256": bundle.deployment_attestation_sha256,
        "deployment_measurement_log_sha256": (bundle.deployment_measurement_log_sha256),
        "mmc_artifacts": dict(bundle.mmc_artifacts),
        "bootargs_sha256": _sha256(boot_stability_bootargs(plan).encode()),
        "deadlines": {
            "selected_command": config.selected_command_timeout,
            "total": config.total_timeout,
            "open": config.open_timeout,
            "artifact": config.artifact_timeout,
            "boot": config.boot_timeout,
            "readiness": config.readiness_timeout,
            "firefox_preflight": config.firefox_preflight_timeout,
            "status": config.status_timeout,
            "snapshot": config.snapshot_timeout,
            "diagnostics": config.diagnostics_timeout,
            "reboot": config.reboot_timeout,
            "recovery": config.recovery_timeout,
        },
        "diagnostic_environment": ["ASTERINAS_MARIONETTE_DIAGNOSTICS=1"],
        "snapshot_limits": {
            "max_seconds": 3,
            "max_processes": 16,
            "max_threads": 128,
            "max_fds": 64,
            "max_file_bytes": 8192,
            "max_total_bytes": 262144,
        },
        "new_session_parameters": {
            "pageLoadStrategy": "none",
            "strictFileInteractability": True,
        },
        "new_session_host_grace_seconds": NEW_SESSION_HOST_GRACE_SECONDS,
    }
    return _sha256(_canonical_json(value))


def _phase_budget(
    clock: Callable[[], float], deadline: float, requested: float
) -> float:
    remaining = deadline - clock()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("Firefox diagnostic total deadline expired")
    return min(requested, remaining)


def _complete_snapshot_payload(value: object, firefox: FirefoxProcessIdentity) -> bytes:
    snapshot = _validate_firefox_snapshot(value, firefox.pid)
    identity = snapshot["root_identity"]
    if (
        snapshot["complete"] is not True
        or snapshot["limitations"]
        or not isinstance(identity, dict)
        or identity["start_time_ticks"] != firefox.start_time_ticks
    ):
        raise HostGateError("Firefox snapshot evidence is incomplete")
    return _canonical_json(snapshot)


def _run_firefox_diagnosis(
    plan: Any,
    config: FirefoxDiagnosticConfig,
    operations: FirefoxDiagnosticOperations,
    publisher: FirefoxDiagnosticPublisher,
    *,
    snapshot_nonces: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
) -> FirefoxDiagnosticResult:
    """Run one admitted physical diagnosis and always recover a started guest."""

    plan.validate()
    bootargs = boot_stability_bootargs(plan)
    inputs = publisher.inputs
    if inputs.plan_sha256 != plan.plan_sha256:
        raise HostGateError("diagnostic publisher plan identity is stale")
    firefox_diagnostic_commands(
        2,
        snapshot_nonces,
        selected_timeout=config.selected_command_timeout,
    )
    publisher.invalidate()
    total_start = clock()
    total_deadline = total_start + config.total_timeout
    transport: tuple[str, ...] = ()
    transfer_bytes = 0
    readiness: BootReadinessEvidence | None = None
    firefox: FirefoxProcessIdentity | None = None
    snapshot_payloads: dict[str, bytes] = {}
    diagnostics = b""
    serial = b""
    recovered = False
    boundary = _boundary_evidence("evidence-incomplete")
    failures: list[str] = []
    interruption: BaseException | None = None
    recovery_failed = False

    try:
        operations.open(_phase_budget(clock, total_deadline, config.open_timeout))
        transport = operations.ensure_artifacts(
            plan,
            _phase_budget(clock, total_deadline, config.artifact_timeout),
        )
        transfer_bytes = operations.artifact_transfer_bytes
        if type(transfer_bytes) is not int or transfer_bytes < 0:
            raise HostGateError("artifact transfer accounting is invalid")
        if transfer_bytes != 0:
            raise HostGateError("unchanged experiment transferred artifact bytes")
        operations.boot(
            plan,
            bootargs,
            _phase_budget(clock, total_deadline, config.boot_timeout),
        )
        readiness = operations.prove_boot_readiness(
            _phase_budget(clock, total_deadline, config.readiness_timeout)
        )
    except Exception as error:
        failures.append(_failure_reason(error))
    except BaseException as error:
        interruption = error
        failures.append(_failure_reason(error))

    if readiness is not None and interruption is None:
        try:
            firefox = operations.firefox_preflight(
                readiness.browser_pid,
                snapshot_nonces,
                config.selected_command_timeout,
                _phase_budget(clock, total_deadline, config.firefox_preflight_timeout),
            )
        except Exception as error:
            failures.append(f"firefox-preflight-{_failure_reason(error)}")
        except BaseException as error:
            interruption = error
            failures.append(_failure_reason(error))

    status_complete = False
    if firefox is not None and interruption is None:
        try:
            operations.run_firefox_status(
                _phase_budget(clock, total_deadline, config.status_timeout)
            )
            status_complete = True
        except Exception as error:
            failures.append(f"status-{_failure_reason(error)}")
        except BaseException as error:
            interruption = error
            failures.append(_failure_reason(error))

    if status_complete and firefox is not None and interruption is None:
        try:
            value = operations.capture_firefox_snapshot(
                "before",
                snapshot_nonces["before"],
                _phase_budget(clock, total_deadline, config.snapshot_timeout),
            )
            snapshot_payloads["before"] = _complete_snapshot_payload(value, firefox)
        except Exception as error:
            failures.append(f"snapshot-before-{_failure_reason(error)}")
        except BaseException as error:
            if interruption is None:
                interruption = error
            failures.append(_failure_reason(error))

        try:
            operations.run_firefox_new_session(
                _phase_budget(
                    clock,
                    total_deadline,
                    config.selected_command_timeout + NEW_SESSION_HOST_GRACE_SECONDS,
                )
            )
        except Exception as error:
            failures.append(f"new-session-{_failure_reason(error)}")
        except BaseException as error:
            interruption = error
            failures.append(_failure_reason(error))

        for phase in ("during", "after"):
            try:
                value = operations.capture_firefox_snapshot(
                    phase,
                    snapshot_nonces[phase],
                    _phase_budget(clock, total_deadline, config.snapshot_timeout),
                )
                snapshot_payloads[phase] = _complete_snapshot_payload(value, firefox)
            except Exception as error:
                failures.append(f"snapshot-{phase}-{_failure_reason(error)}")
            except BaseException as error:
                if interruption is None:
                    interruption = error
                failures.append(_failure_reason(error))

    if operations.guest_started:
        try:
            collected = operations.collect_diagnostics(
                _phase_budget(clock, total_deadline, config.diagnostics_timeout)
            )
            if not isinstance(collected, bytes):
                raise HostGateError("Firefox diagnostics must be bytes")
            diagnostics = collected
        except Exception as error:
            failures.append(f"diagnostics-{_failure_reason(error)}")
        except BaseException as error:
            if interruption is None:
                interruption = error
            failures.append(_failure_reason(error))

        try:
            operations.request_reboot(config.reboot_timeout)
        except Exception as error:
            failures.append(f"reboot-{_failure_reason(error)}")
        except BaseException as error:
            if interruption is None:
                interruption = error
            failures.append(_failure_reason(error))
        try:
            operations.await_recovery(config.recovery_timeout)
            recovered = True
        except Exception as error:
            recovery_failed = True
            failures.append(f"recovery-{_failure_reason(error)}")
        except BaseException as error:
            recovery_failed = True
            if interruption is None:
                interruption = error
            failures.append(_failure_reason(error))

    try:
        serial = _transcript_bytes(operations.transcript)
        boundary = classify_new_session_transcript(serial)
    except Exception as error:
        failures.append(f"transport-{_failure_reason(error)}")
    except BaseException as error:
        if interruption is None:
            interruption = error
        failures.append(_failure_reason(error))
    if boundary.boundary == "evidence-incomplete":
        failures.append("transport-evidence-incomplete")
    if firefox is None:
        failures.append("firefox-identity-incomplete")
    if set(snapshot_payloads) != set(_SNAPSHOT_PHASES):
        failures.append("snapshot-evidence-incomplete")
    if operations.guest_started and not recovered:
        failures.append("recovery-incomplete")

    passed = not failures and interruption is None
    if passed:
        reason = "firefox-diagnosis-complete"
    elif recovery_failed:
        reason = "manual-reset-required"
    else:
        reason = "firefox-diagnosis-incomplete"
    result = FirefoxDiagnosticResult(
        schema_version=1,
        passed=passed,
        physical=True,
        reason=reason,
        failure=";".join(dict.fromkeys(failures)),
        experiment_sha256=inputs.experiment_sha256,
        bundle_sha256=inputs.bundle_sha256,
        plan_sha256=inputs.plan_sha256,
        deployment_attestation_sha256=inputs.deployment_attestation_sha256,
        deployment_measurement_log_sha256=(inputs.deployment_measurement_log_sha256),
        bootargs_sha256=_sha256(bootargs.encode()),
        hypothesis=config.hypothesis,
        contrary_outcome=config.contrary_outcome,
        artifact_transfer_bytes=transfer_bytes,
        qemu_runs=0,
        physical_boots=1 if operations.guest_started else 0,
        total_host_seconds=_elapsed_seconds(clock, total_start),
        boundary=boundary,
        snapshot_sha256=tuple(
            _sha256(snapshot_payloads.get(phase, b"")) for phase in _SNAPSHOT_PHASES
        ),
        diagnostics_sha256=_sha256(diagnostics),
        serial_sha256=_sha256(serial),
        firefox=firefox,
        transport=transport,
        recovered=recovered,
    )
    publisher.publish(result, serial, diagnostics, snapshot_payloads)
    if interruption is not None:
        raise interruption
    return result


def run_firefox_diagnosis(
    plan: Any,
    config: FirefoxDiagnosticConfig,
    operations: FirefoxDiagnosticOperations,
    publisher: FirefoxDiagnosticPublisher,
    *,
    snapshot_nonces: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
) -> FirefoxDiagnosticResult:
    """Close the host serial descriptor on every terminal path."""

    try:
        return _run_firefox_diagnosis(
            plan,
            config,
            operations,
            publisher,
            snapshot_nonces=snapshot_nonces,
            clock=clock,
        )
    finally:
        operations.close()


class RealFirefoxDiagnosticPublisher:
    """Race-safe one-run admission and private atomic evidence publisher."""

    _OUTPUT_NAMES = (
        "bundle.json",
        "physical.serial.log",
        "diagnostics.log",
        "snapshot-before.json",
        "snapshot-during.json",
        "snapshot-after.json",
        "sha256sums.txt",
        "result.json",
    )
    _MAX_LEDGER_BYTES = 1024 * 1024

    def __init__(
        self,
        bundle: DesktopBundle,
        plan: Any,
        config: FirefoxDiagnosticConfig,
        output_directory: Path,
    ) -> None:
        identity = experiment_identity(bundle, plan, config)
        self._bundle = bundle
        self._output_directory = output_directory
        self._evidence_root = Path(bundle.evidence_root)
        self._inputs = FirefoxDiagnosticInputs(
            experiment_sha256=identity,
            bundle_sha256=_sha256(bundle.canonical_bytes()),
            plan_sha256=plan.plan_sha256,
            deployment_attestation_sha256=bundle.deployment_attestation_sha256,
            deployment_measurement_log_sha256=(
                bundle.deployment_measurement_log_sha256
            ),
        )
        self._output: PinnedOutputDirectory | None = None

    @property
    def inputs(self) -> FirefoxDiagnosticInputs:
        return self._inputs

    def _admit_once(self) -> None:
        ledger = self._evidence_root / "experiments.jsonl"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(ledger, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > self._MAX_LEDGER_BYTES
            ):
                raise HostGateError("Firefox experiment ledger is invalid")
            os.lseek(descriptor, 0, os.SEEK_SET)
            payload = bytearray()
            while len(payload) <= self._MAX_LEDGER_BYTES:
                chunk = os.read(descriptor, self._MAX_LEDGER_BYTES + 1 - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
            try:
                lines = payload.decode("ascii").splitlines()
            except UnicodeDecodeError as error:
                raise HostGateError("Firefox experiment ledger is invalid") from error
            for line in lines:
                try:
                    value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                except (json.JSONDecodeError, ValueError) as error:
                    raise HostGateError(
                        "Firefox experiment ledger is invalid"
                    ) from error
                if (
                    not isinstance(value, dict)
                    or set(value) != {"schema_version", "experiment_sha256", "state"}
                    or type(value["schema_version"]) is not int
                    or value["schema_version"] != 1
                    or _SHA256.fullmatch(value["experiment_sha256"]) is None
                    or value["state"] != "admitted"
                    or _canonical_json(value).decode("ascii").rstrip("\n") != line
                ):
                    raise HostGateError("Firefox experiment ledger is invalid")
                if value["experiment_sha256"] == self._inputs.experiment_sha256:
                    raise HostGateError(
                        "Firefox experiment identity was already executed"
                    )
            record = _canonical_json(
                {
                    "schema_version": 1,
                    "experiment_sha256": self._inputs.experiment_sha256,
                    "state": "admitted",
                }
            )
            if len(payload) + len(record) > self._MAX_LEDGER_BYTES:
                raise HostGateError("Firefox experiment ledger is full")
            os.lseek(descriptor, 0, os.SEEK_END)
            written = 0
            while written < len(record):
                written += os.write(descriptor, record[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def invalidate(self) -> None:
        if self._output is not None:
            raise HostGateError("Firefox diagnostic output is already active")
        if self._output_directory.parent != self._evidence_root:
            raise HostGateError("Firefox diagnostic output must be under evidence root")
        self._evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._evidence_root.is_symlink() or not self._evidence_root.is_dir():
            raise HostGateError("Firefox evidence root is unsafe")
        if self._evidence_root.stat().st_mode & 0o077:
            raise HostGateError("Firefox evidence root must be private")
        if self._output_directory.exists():
            raise HostGateError("Firefox diagnostic output directory already exists")
        self._admit_once()
        self._output_directory.mkdir(mode=0o700)
        output = PinnedOutputDirectory(self._output_directory)
        try:
            output.lock_exclusive()
        except BaseException:
            output.close()
            raise
        self._output = output

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def publish(
        self,
        result: FirefoxDiagnosticResult,
        serial: bytes,
        diagnostics: bytes,
        snapshots: Mapping[str, bytes],
    ) -> None:
        if self._output is None:
            raise HostGateError("Firefox diagnostic output is not pinned")
        if (
            result.experiment_sha256 != self._inputs.experiment_sha256
            or result.bundle_sha256 != self._inputs.bundle_sha256
            or result.plan_sha256 != self._inputs.plan_sha256
            or not isinstance(serial, bytes)
            or len(serial) > 8 * 1024 * 1024
            or not isinstance(diagnostics, bytes)
            or len(diagnostics) > 256 * 1024
            or not set(snapshots).issubset(_SNAPSHOT_PHASES)
            or (result.passed and set(snapshots) != set(_SNAPSHOT_PHASES))
            or any(
                not isinstance(payload, bytes)
                or not 0 < len(payload) <= MAX_FIREFOX_SNAPSHOT_BYTES
                for payload in snapshots.values()
            )
            or result.serial_sha256 != _sha256(serial)
            or result.diagnostics_sha256 != _sha256(diagnostics)
            or result.snapshot_sha256
            != tuple(_sha256(snapshots.get(phase, b"")) for phase in _SNAPSHOT_PHASES)
        ):
            raise HostGateError("Firefox diagnostic publication identity is invalid")
        output, self._output = self._output, None
        try:
            payloads = {
                "bundle.json": self._bundle.canonical_bytes(),
                "physical.serial.log": serial,
                "diagnostics.log": diagnostics,
                **{
                    f"snapshot-{phase}.json": payload
                    for phase, payload in snapshots.items()
                },
            }
            for name, payload in payloads.items():
                output.atomic_write(name, payload, mode=0o600)
            result_payload = result.canonical_bytes()
            sums = [f"{_sha256(payload)}  {name}" for name, payload in payloads.items()]
            sums.append(f"{_sha256(result_payload)}  result.json")
            output.atomic_write(
                "sha256sums.txt", ("\n".join(sums) + "\n").encode(), mode=0o600
            )
            output.atomic_write("result.json", result_payload, mode=0o600)
        finally:
            output.close()


_FIREFOX_PREFLIGHT_MARKER = re.compile(
    r"\A__ASTERINAS_FIREFOX_PREFLIGHT__ pid=([0-9]+) start=([0-9]+) "
    r"restarts=([0-9]+) profile=([0-9]+:[0-9]+)\Z"
)
_FIREFOX_STATUS_MARKER = re.compile(r"\A__ASTERINAS_FIREFOX_STATUS__ status=([0-9]+)\Z")
_FIREFOX_NEW_SESSION_MARKER = re.compile(
    r"\A__ASTERINAS_FIREFOX_NEW_SESSION__ status=([0-9]+)\Z"
)
_FIREFOX_TERMINAL_MARKER = re.compile(
    r"\A__ASTERINAS_FIREFOX_TERMINAL__ pid=([0-9]+) start=([0-9]+) "
    r"restarts=([0-9]+) profile=([0-9]+:[0-9]+)\Z"
)


class RealFirefoxDiagnosticOperations(RealBootCycleOperations):
    """MMC-only board adapter for the short Firefox diagnostic protocol."""

    @property
    def artifact_transfer_bytes(self) -> int:
        return 0

    def ensure_artifacts(self, plan: Any, timeout: float) -> tuple[str, ...]:
        if self._mmc_artifacts is None:
            raise HostGateError("Firefox diagnosis requires existing MMC artifacts")
        outcomes = super().ensure_artifacts(plan, timeout)
        if outcomes != ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc"):
            raise HostGateError("Firefox diagnosis attempted a non-MMC transport")
        return outcomes

    def _run_diagnostic_command(self, expected_index: int, timeout: float) -> None:
        commands = getattr(self, "_firefox_commands", None)
        cursor_index = getattr(self, "_firefox_command_index", 0)
        if commands is None or cursor_index != expected_index:
            raise HostGateError("Firefox diagnostic command order is invalid")
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        nonce = secrets.token_hex(8)
        marker = (
            f"__ASTERINAS_FIREFOX_COMMAND__ nonce={nonce} step={expected_index} done=1"
        )
        cursor = serial.checkpoint()
        try:
            # Send the command and its ACK as one canonical-mode input line.
            # A second write can be echoed while the first command is already
            # producing output, corrupting an otherwise valid protocol line.
            self._send_bounded(
                serial,
                f"{commands[expected_index]}; printf '{marker}\\n'",
                deadline,
            )
            while True:
                line, cursor = self._next_line(serial, cursor, deadline)
                if line == marker:
                    break
        except TimeoutError:
            self._abort_guest_shell()
            raise
        self._firefox_command_index = expected_index + 1
        self._sync_serial_log()

    def _single_marker(self, pattern: re.Pattern[str], label: str) -> re.Match[str]:
        text = _transcript_bytes(self.transcript).decode("utf-8")
        matches = [pattern.fullmatch(line.rstrip("\r")) for line in text.split("\n")]
        selected = [match for match in matches if match is not None]
        if len(selected) != 1:
            raise HostGateError(f"Firefox {label} marker is missing or duplicated")
        return selected[0]

    def firefox_preflight(
        self,
        browser_pid: int,
        snapshot_nonces: Mapping[str, str],
        selected_timeout: float,
        timeout: float,
    ) -> FirefoxProcessIdentity:
        self._firefox_commands = firefox_diagnostic_commands(
            browser_pid,
            snapshot_nonces,
            selected_timeout=selected_timeout,
        )
        self._firefox_command_index = 0
        deadline = time.monotonic() + timeout
        for index in range(4):
            self._run_diagnostic_command(
                index, _phase_budget(time.monotonic, deadline, timeout)
            )
        marker = self._single_marker(_FIREFOX_PREFLIGHT_MARKER, "preflight")
        identity = FirefoxProcessIdentity(
            pid=int(marker.group(1)),
            start_time_ticks=int(marker.group(2)),
            browser_restarts=int(marker.group(3)),
            profile_identity=marker.group(4),
        )
        if identity.pid != browser_pid:
            raise HostGateError("Firefox preflight changed the readiness PID")
        self._firefox_identity = identity
        return identity

    def run_firefox_status(self, timeout: float) -> None:
        self._run_diagnostic_command(4, timeout)
        marker = self._single_marker(_FIREFOX_STATUS_MARKER, "Status")
        if marker.group(1) != "0":
            raise HostGateError(
                f"Firefox Status failed with guest status {marker.group(1)}"
            )

    def capture_firefox_snapshot(
        self, phase: str, nonce: str, timeout: float
    ) -> dict[str, object]:
        if phase == "before":
            indexes = (5, 6)
        elif phase == "during":
            indexes = (9, 10)
        elif phase == "after":
            indexes = (11, 12, 13, 14, 15, 16)
        else:
            raise ValueError("Firefox snapshot phase is invalid")
        deadline = time.monotonic() + timeout
        for index in indexes:
            self._run_diagnostic_command(
                index, _phase_budget(time.monotonic, deadline, timeout)
            )
        browser_pid = int(getattr(self, "_browser_pid", 0) or 0)
        snapshot = parse_firefox_snapshot_frame(
            self.transcript,
            phase,
            nonce,
            expected_root_pid=browser_pid,
        )
        if phase == "after":
            marker = self._single_marker(_FIREFOX_TERMINAL_MARKER, "terminal identity")
            identity = getattr(self, "_firefox_identity", None)
            if identity is None or (
                int(marker.group(1)),
                int(marker.group(2)),
                int(marker.group(3)),
                marker.group(4),
            ) != (
                identity.pid,
                identity.start_time_ticks,
                identity.browser_restarts,
                identity.profile_identity,
            ):
                raise HostGateError("Firefox terminal identity changed")
        return snapshot

    def run_firefox_new_session(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        self._run_diagnostic_command(
            7,
            _phase_budget(time.monotonic, deadline, NEW_SESSION_HOST_GRACE_SECONDS),
        )
        self._run_diagnostic_command(
            8, _phase_budget(time.monotonic, deadline, timeout)
        )
        self._single_marker(_FIREFOX_NEW_SESSION_MARKER, "NewSession")


class RealDesktopStartPublisher:
    """Private atomic evidence publisher for a desktop left running."""

    def __init__(
        self,
        bundle: DesktopBundle,
        output_directory: Path,
    ) -> None:
        self._bundle = bundle
        self._output_directory = output_directory
        self._evidence_root = Path(bundle.evidence_root)
        self._output: PinnedOutputDirectory | None = None

    def invalidate(self) -> None:
        if self._output is not None:
            raise HostGateError("desktop-start output is already active")
        if self._output_directory.parent != self._evidence_root:
            raise HostGateError("desktop-start output must be under evidence root")
        self._evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._evidence_root.is_symlink() or not self._evidence_root.is_dir():
            raise HostGateError("desktop-start evidence root is unsafe")
        if self._evidence_root.stat().st_mode & 0o077:
            raise HostGateError("desktop-start evidence root must be private")
        if self._output_directory.exists():
            raise HostGateError("desktop-start output directory already exists")
        self._output_directory.mkdir(mode=0o700)
        output = PinnedOutputDirectory(self._output_directory)
        try:
            output.lock_exclusive()
        except BaseException:
            output.close()
            raise
        self._output = output

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def publish(
        self, result: DesktopStartResult, serial: bytes, diagnostics: bytes
    ) -> None:
        if self._output is None:
            raise HostGateError("desktop-start output is not pinned")
        if (
            result.plan_sha256 != self._bundle.plan_sha256
            or not isinstance(serial, bytes)
            or len(serial) > 8 * 1024 * 1024
            or not isinstance(diagnostics, bytes)
            or len(diagnostics) > 256 * 1024
            or result.serial_sha256 != _sha256(serial)
            or result.diagnostics_sha256 != _sha256(diagnostics)
        ):
            raise HostGateError("desktop-start publication identity is invalid")
        output, self._output = self._output, None
        try:
            payloads = {
                "bundle.json": self._bundle.canonical_bytes(),
                "physical.serial.log": serial,
                "diagnostics.log": diagnostics,
            }
            for name, payload in payloads.items():
                output.atomic_write(name, payload, mode=0o600)
            result_payload = result.canonical_bytes()
            sums = [f"{_sha256(payload)}  {name}" for name, payload in payloads.items()]
            sums.append(f"{_sha256(result_payload)}  result.json")
            output.atomic_write(
                "sha256sums.txt", ("\n".join(sums) + "\n").encode(), mode=0o600
            )
            output.atomic_write("result.json", result_payload, mode=0o600)
        finally:
            output.close()


def parse_args(arguments: list[str] | tuple[str, ...]) -> argparse.Namespace:
    """Parse the configure-once, start, and bounded diagnostic actions."""

    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)

    configure = actions.add_parser("configure")
    configure.add_argument("--plan", required=True, type=Path)
    configure.add_argument("--device", required=True)
    configure.add_argument("--deployment-attestation", required=True, type=Path)
    configure.add_argument("--deployment-measurement-log", required=True, type=Path)
    configure.add_argument("--mmc-kernel", required=True, type=safe_artifact_name)
    configure.add_argument("--mmc-initramfs", required=True, type=safe_artifact_name)
    configure.add_argument("--mmc-dtb", required=True, type=safe_artifact_name)
    configure.add_argument("--evidence-root", required=True, type=Path)
    configure.add_argument("--output", required=True, type=Path)

    start = actions.add_parser("start")
    start.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)

    diagnose = actions.add_parser("diagnose-firefox")
    diagnose.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    diagnose.add_argument("--hypothesis", required=True)
    diagnose.add_argument("--contrary-outcome", required=True)
    return parser.parse_args(arguments)


def _resolved_bundle_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    repository = Path(__file__).resolve().parents[2]
    return (repository / path).absolute()


def _fresh_run_directory(bundle: DesktopBundle, action: str) -> Path:
    if action not in {"start", "firefox"}:
        raise ValueError("desktop run action is invalid")
    return Path(bundle.evidence_root) / f"{action}-{secrets.token_hex(8)}"


def main(arguments: list[str] | tuple[str, ...] | None = None) -> int:
    """Run one local-only configured desktop action."""

    values = parse_args(tuple(sys.argv[1:]) if arguments is None else arguments)
    publisher: RealDesktopStartPublisher | RealFirefoxDiagnosticPublisher | None = None
    try:
        if values.action == "configure":
            bundle = configure_bundle(
                plan_path=values.plan,
                device=values.device,
                deployment_attestation_path=values.deployment_attestation,
                deployment_measurement_log_path=values.deployment_measurement_log,
                mmc_artifacts={
                    "kernel": values.mmc_kernel,
                    "initramfs": values.mmc_initramfs,
                    "megrez_dtb": values.mmc_dtb,
                },
                evidence_root=values.evidence_root,
                destination=values.output,
            )
            print(bundle.canonical_bytes().decode("ascii"), end="")
            return 0

        bundle_path = _resolved_bundle_path(values.bundle)
        bundle = DesktopBundle.from_path(bundle_path)
        plan = _read_plan(Path(bundle.plan_path))
        plan.validate()
        if plan.plan_sha256 != bundle.plan_sha256:
            raise HostGateError("desktop plan changed after bundle validation")
        run_directory = _fresh_run_directory(
            bundle, "start" if values.action == "start" else "firefox"
        )
        unused_hdmi = run_directory / ".unused-hdmi-capture"
        if values.action == "start":
            publisher = RealDesktopStartPublisher(bundle, run_directory)
            operations = RealBootCycleOperations(
                plan,
                bundle.device,
                run_directory,
                unused_hdmi,
                mmc_artifacts=bundle.mmc_artifacts,
            )
            result = run_desktop_start(
                plan,
                DesktopStartConfig(),
                operations,
                publisher,
            )
        else:
            config = FirefoxDiagnosticConfig(
                hypothesis=values.hypothesis,
                contrary_outcome=values.contrary_outcome,
            )
            publisher = RealFirefoxDiagnosticPublisher(
                bundle, plan, config, run_directory
            )
            operations = RealFirefoxDiagnosticOperations(
                plan,
                bundle.device,
                run_directory,
                unused_hdmi,
                mmc_artifacts=bundle.mmc_artifacts,
            )
            result = run_firefox_diagnosis(
                plan,
                config,
                operations,
                publisher,
                snapshot_nonces={
                    phase: secrets.token_hex(8) for phase in _SNAPSHOT_PHASES
                },
            )
    except (HostGateError, OSError, RuntimeError, ValueError) as error:
        print(f"Megrez desktop action failed: {error}", file=sys.stderr)
        return 2
    finally:
        if publisher is not None:
            publisher.close()
    print(result.canonical_bytes().decode("ascii"), end="")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
