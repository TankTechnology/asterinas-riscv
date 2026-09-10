#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Start and diagnose the configured MMC-backed Megrez Debian desktop."""

from __future__ import annotations

import argparse
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
