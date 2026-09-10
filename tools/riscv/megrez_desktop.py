#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Start and diagnose the configured MMC-backed Megrez Debian desktop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Mapping

from tools.riscv.megrez_board_session import safe_artifact_name
from tools.riscv.megrez_boot_stability import _read_deployment_attestation
from tools.riscv.megrez_physical_graphics import _read_plan


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
            actual = _sha256(
                _read_bounded_regular(Path(input_path), label, maximum)
            )
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
