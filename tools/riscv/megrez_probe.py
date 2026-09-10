#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded kernel probes for the current Megrez MMC deployment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Sequence

from tools.riscv.megrez_debug_contract import DebugContractError, DebugPlan


_BUNDLE_FIELDS = frozenset(
    ("schema_version", "plan", "plan_sha256", "device", "mmc_artifacts")
)
_MMC_FIELDS = frozenset(("name", "path"))
_MMC_ORDER = ("kernel", "initramfs", "megrez_dtb")
_MMC_PATH = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*"
)
_SERIAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProbeContractError(ValueError):
    """A fast-probe input does not satisfy the immutable contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProbeContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(data: bytes) -> object:
    if not isinstance(data, bytes):
        raise ProbeContractError("bundle must be bytes")
    try:
        return json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeContractError("bundle is not canonical JSON") from error


def _exact_mapping(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProbeContractError(f"{label} fields do not match the contract")
    if any(not isinstance(key, str) for key in value):
        raise ProbeContractError(f"{label} field names must be strings")
    return value


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True)
class MmcArtifact:
    """One safe partition-1 filename selected for U-Boot loading."""

    name: str
    path: str

    @classmethod
    def from_mapping(cls, value: object) -> MmcArtifact:
        mapping = _exact_mapping(value, _MMC_FIELDS, "MMC artifact")
        artifact = cls(name=mapping["name"], path=mapping["path"])
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if self.name not in _MMC_ORDER:
            raise ProbeContractError("unknown MMC artifact name")
        if not isinstance(self.path, str) or _MMC_PATH.fullmatch(self.path) is None:
            raise ProbeContractError("unsafe MMC path")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {"name": self.name, "path": self.path}


@dataclass(frozen=True)
class ProbeBundle:
    """One exact deployment selected for repeatable physical probes."""

    schema_version: int
    plan: DebugPlan
    plan_sha256: str
    device: str
    mmc_artifacts: tuple[MmcArtifact, ...]

    @classmethod
    def from_bytes(cls, data: bytes) -> ProbeBundle:
        mapping = _exact_mapping(_load_json(data), _BUNDLE_FIELDS, "probe bundle")
        try:
            plan = DebugPlan.from_bytes(_canonical_json(mapping["plan"]))
        except (DebugContractError, TypeError, ValueError) as error:
            raise ProbeContractError(f"invalid embedded debug plan: {error}") from error
        artifacts = mapping["mmc_artifacts"]
        if not isinstance(artifacts, list):
            raise ProbeContractError("MMC artifacts must be an array")
        bundle = cls(
            schema_version=mapping["schema_version"],
            plan=plan,
            plan_sha256=mapping["plan_sha256"],
            device=mapping["device"],
            mmc_artifacts=tuple(MmcArtifact.from_mapping(item) for item in artifacts),
        )
        bundle.validate()
        return bundle

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProbeContractError("probe bundle schema must be 1")
        try:
            self.plan.validate()
        except DebugContractError as error:
            raise ProbeContractError(f"invalid embedded debug plan: {error}") from error
        if (
            not isinstance(self.plan_sha256, str)
            or _SHA256.fullmatch(self.plan_sha256) is None
            or self.plan_sha256 != self.plan.plan_sha256
        ):
            raise ProbeContractError("plan digest does not match embedded plan")
        if not isinstance(self.device, str):
            raise ProbeContractError("unsafe serial device")
        device = Path(self.device)
        if (
            device.parent != Path("/dev/serial/by-id")
            or _SERIAL_NAME.fullmatch(device.name) is None
        ):
            raise ProbeContractError("unsafe serial device")
        if (
            not isinstance(self.mmc_artifacts, tuple)
            or tuple(item.name for item in self.mmc_artifacts) != _MMC_ORDER
        ):
            raise ProbeContractError("MMC artifacts must use canonical order")
        identities = {identity.name: identity for identity in self.plan.artifacts}
        for artifact in self.mmc_artifacts:
            artifact.validate()
            if artifact.name not in identities:
                raise ProbeContractError("MMC artifact is absent from embedded plan")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "plan": self.plan.to_dict(),
            "plan_sha256": self.plan_sha256,
            "device": self.device,
            "mmc_artifacts": [artifact.to_dict() for artifact in self.mmc_artifacts],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @property
    def bundle_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class ProbeDefinition:
    """One fixed Stage1 probe and its internal maximum duration."""

    name: str
    timeout_seconds: int


_PROBE_REGISTRY = {
    definition.name: definition
    for definition in (
        ProbeDefinition("boot", 5),
        ProbeDefinition("syscall213", 5),
        ProbeDefinition("syscall272", 5),
        ProbeDefinition("ext2-writeback", 15),
        ProbeDefinition("systemd-compat", 10),
    )
}


def validate_probe_names(names: Sequence[str]) -> tuple[ProbeDefinition, ...]:
    """Resolve one nonempty, duplicate-free ordered probe selection."""

    if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
        raise ProbeContractError("probe names must be a sequence")
    if not 1 <= len(names) <= len(_PROBE_REGISTRY):
        raise ProbeContractError("probe selection must contain one to five names")
    if any(not isinstance(name, str) or name not in _PROBE_REGISTRY for name in names):
        raise ProbeContractError("unknown probe name")
    if len(set(names)) != len(names):
        raise ProbeContractError("duplicate probe name")
    return tuple(_PROBE_REGISTRY[name] for name in names)


def validate_session_seconds(value: object | None) -> int:
    """Return the fixed total guest lifetime in the accepted range."""

    if value is None:
        return 90
    if type(value) is not int or not 30 <= value <= 300:
        raise ProbeContractError("session duration must be an integer from 30 to 300")
    return value
