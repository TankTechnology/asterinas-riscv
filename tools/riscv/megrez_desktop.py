#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Start and diagnose the configured MMC-backed Megrez Debian desktop."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import stat
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from tools.riscv.megrez_board_session import safe_artifact_name
from tools.riscv.megrez_boot_stability import (
    BootReadinessEvidence,
    _read_deployment_attestation,
    contains_fatal_diagnostics,
)
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
        for input_path, expected, label, maximum in (
            (bundle.plan_path, bundle.plan_sha256, "plan", MAX_PLAN_BYTES),
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
            actual = _sha256(_read_bounded_regular(Path(input_path), label, maximum))
            if actual != expected:
                raise ValueError(f"{label} changed after bundle configuration")
        return bundle


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
    plan = _read_plan(plan_path)
    plan.validate()
    attestation = _read_deployment_attestation(deployment_attestation_path)
    plan_digest = _sha256(payloads["plan"])
    if plan.plan_sha256 != plan_digest:
        raise ValueError("plan identity does not match its canonical bytes")
    if attestation.plan_sha256 != plan.plan_sha256:
        raise ValueError("deployment attestation does not match the plan")
    measurement_digest = _sha256(payloads["deployment measurement log"])
    attested_measurement = getattr(attestation, "measurement_log_sha256", None)
    if attested_measurement is not None and attested_measurement != measurement_digest:
        raise ValueError("deployment measurement log does not match the attestation")
    bundle = DesktopBundle(
        schema_version=BUNDLE_SCHEMA_VERSION,
        device=validated_device,
        plan_path=str(plan_path),
        plan_sha256=plan_digest,
        deployment_attestation_path=str(deployment_attestation_path),
        deployment_attestation_sha256=_sha256(payloads["deployment attestation"]),
        deployment_measurement_log_path=str(deployment_measurement_log_path),
        deployment_measurement_log_sha256=measurement_digest,
        mmc_artifacts=validated_mmc,
        evidence_root=str(evidence_root),
    )
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


def run_desktop_start(
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
        diagnostics_seconds = _elapsed_seconds(clock, diagnostics_start)

        recovery_start = clock()
        try:
            operations.request_reboot(config.reboot_timeout)
        except Exception as error:
            failure_details.append(f"reboot-{_failure_reason(error)}")
        try:
            operations.await_recovery(config.recovery_timeout)
            recovered = True
        except Exception as error:
            recovery_failed = True
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
    try:
        publisher.publish(result, serial, diagnostics)
    finally:
        operations.close()
    if interruption is not None:
        raise interruption
    return result


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
        stream.append(record)
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
        if first_positions[new_session_keys[0]] <= first_positions[status_keys[0]]:
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
        '_asterinas_firefox_snapshot >"$_asterinas_firefox_base.after"; '
        "_asterinas_firefox_after_status=$?; "
        "printf '__ASTERINAS_FIREFOX_SNAPSHOT_STATUS__ phase=after status=%s\\n' "
        '"$_asterinas_firefox_after_status"; :',
        "_asterinas_firefox_terminal=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null); "
        "_asterinas_firefox_terminal_restarts=$(systemctl show --property NRestarts "
        "--value asterinas-browser-web.service 2>/dev/null); "
        "_asterinas_firefox_terminal_start=$(cut -d' ' -f22 "
        '"/proc/$_asterinas_firefox_pid/stat" 2>/dev/null); '
        'if [ "$_asterinas_firefox_terminal" = "$_asterinas_firefox_pid" ] && '
        '[ "$_asterinas_firefox_terminal_restarts" = 0 ] && '
        '[ "$_asterinas_firefox_terminal_start" = "$_asterinas_firefox_start" ]; '
        "then printf '__ASTERINAS_FIREFOX_TERMINAL__ pid=%s start=%s restarts=%s\\n' "
        '"$_asterinas_firefox_pid" "$_asterinas_firefox_terminal_start" '
        '"$_asterinas_firefox_terminal_restarts"; else false; fi',
        *(
            _snapshot_frame_command(phase, snapshot_nonces[phase])
            for phase in _SNAPSHOT_PHASES
        ),
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
