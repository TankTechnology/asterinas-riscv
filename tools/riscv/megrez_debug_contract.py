# SPDX-License-Identifier: MPL-2.0

"""Immutable identities shared by Megrez simulation and physical debug runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
ARTIFACT_NAMES = frozenset(("kernel", "initramfs", "qemu_dtb", "megrez_dtb"))
ARTIFACT_ORDER = ("kernel", "initramfs", "qemu_dtb", "megrez_dtb")
PLAN_FIELDS = frozenset(
    (
        "schema_version",
        "profile",
        "artifacts",
        "bootargs",
        "smp",
        "sv39",
        "markers",
        "reboot_after",
    )
)
ARTIFACT_FIELDS = frozenset(("name", "path", "load_address", "size", "sha256", "crc32"))
RESULT_FIELDS = frozenset(
    ("schema_version", "stage", "passed", "reason", "plan_sha256", "evidence")
)
BOOTARGS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._=/,:@+%~-]*")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
CRC32_PATTERN = re.compile(r"[0-9a-f]{8}")
RESULT_STAGES = frozenset(("check", "fast", "desktop", "board"))


class DebugContractError(ValueError):
    """One stable launch-plan identity violation."""


@dataclass(frozen=True)
class ArtifactIdentity:
    """The bytes and load address of one debug artifact."""

    name: str
    path: str
    load_address: int
    size: int
    sha256: str
    crc32: str

    @classmethod
    def from_path(cls, name: str, path: Path, load_address: int) -> ArtifactIdentity:
        if name not in ARTIFACT_NAMES:
            raise DebugContractError("unknown artifact name")
        if (
            isinstance(load_address, bool)
            or not isinstance(load_address, int)
            or load_address <= 0
            or load_address % 4
        ):
            raise DebugContractError("load address must be positive and 4-byte aligned")

        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise DebugContractError("artifact must be a regular non-symlink file")
        if before.st_size == 0:
            raise DebugContractError("artifact is empty")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise DebugContractError("artifact exceeds the 64 MiB limit")

        digest = hashlib.sha256()
        crc = 0
        size = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                raise DebugContractError("artifact identity changed before open")
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_ARTIFACT_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise DebugContractError("artifact exceeds the 64 MiB limit")
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)

        if size != opened.st_size:
            raise DebugContractError("artifact size changed while reading")
        return cls(
            name=name,
            path=str(path.absolute()),
            load_address=load_address,
            size=size,
            sha256=digest.hexdigest(),
            crc32=f"{crc:08x}",
        )

    def validate(self) -> None:
        if self.name not in ARTIFACT_NAMES:
            raise DebugContractError("unknown artifact name")
        if not isinstance(self.path, str) or not Path(self.path).is_absolute():
            raise DebugContractError("artifact path must be absolute")
        if (
            isinstance(self.load_address, bool)
            or not isinstance(self.load_address, int)
            or self.load_address <= 0
            or self.load_address % 4
        ):
            raise DebugContractError("invalid artifact load address")
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or not 0 < self.size <= MAX_ARTIFACT_BYTES
        ):
            raise DebugContractError("invalid artifact size")
        if (
            not isinstance(self.sha256, str)
            or SHA256_PATTERN.fullmatch(self.sha256) is None
        ):
            raise DebugContractError("invalid artifact SHA-256")
        if (
            not isinstance(self.crc32, str)
            or CRC32_PATTERN.fullmatch(self.crc32) is None
        ):
            raise DebugContractError("invalid artifact CRC32")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "name": self.name,
            "path": self.path,
            "load_address": self.load_address,
            "size": self.size,
            "sha256": self.sha256,
            "crc32": self.crc32,
        }

    @classmethod
    def from_dict(cls, value: object) -> ArtifactIdentity:
        mapping = _exact_mapping(value, ARTIFACT_FIELDS, "artifact")
        identity = cls(
            name=mapping["name"],
            path=mapping["path"],
            load_address=mapping["load_address"],
            size=mapping["size"],
            sha256=mapping["sha256"],
            crc32=mapping["crc32"],
        )
        identity.validate()
        return identity


@dataclass(frozen=True)
class DebugPlan:
    """One exact artifact set shared by simulation and physical boot."""

    schema_version: int
    profile: str
    artifacts: tuple[ArtifactIdentity, ...]
    bootargs: str
    smp: int
    sv39: bool
    markers: tuple[str, ...]
    reboot_after: int

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise DebugContractError("debug plan schema must be 1")
        if self.profile != "tcp-probe":
            raise DebugContractError("unsupported debug profile")
        if (
            not isinstance(self.artifacts, tuple)
            or tuple(
                identity.name
                for identity in self.artifacts
                if isinstance(identity, ArtifactIdentity)
            )
            != ARTIFACT_ORDER
            or len(self.artifacts) != len(ARTIFACT_ORDER)
        ):
            raise DebugContractError("artifacts must use the canonical order")
        for identity in self.artifacts:
            if not isinstance(identity, ArtifactIdentity):
                raise DebugContractError("invalid artifact entry")
            identity.validate()
        if (
            not isinstance(self.bootargs, str)
            or BOOTARGS_PATTERN.fullmatch(self.bootargs) is None
        ):
            raise DebugContractError("unsafe debug bootargs")
        if type(self.smp) is not int or self.smp != 4:
            raise DebugContractError("debug plan requires SMP=4")
        if self.sv39 is not True:
            raise DebugContractError("debug plan requires Sv39")
        if (
            not isinstance(self.markers, tuple)
            or not self.markers
            or len(set(self.markers)) != len(self.markers)
            or any(not _safe_text(marker) for marker in self.markers)
        ):
            raise DebugContractError("debug markers must be unique safe strings")
        if (
            type(self.reboot_after) is not int
            or not 1 <= self.reboot_after <= 0xFFFF_FFFF
        ):
            raise DebugContractError("invalid automatic reboot interval")
        token = f"asterinas.reboot_after={self.reboot_after}"
        if self.bootargs.split().count(token) != 1:
            raise DebugContractError("bootargs do not match automatic reboot interval")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "artifacts": [identity.to_dict() for identity in self.artifacts],
            "bootargs": self.bootargs,
            "smp": self.smp,
            "sv39": self.sv39,
            "markers": list(self.markers),
            "reboot_after": self.reboot_after,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, data: bytes) -> DebugPlan:
        mapping = _load_exact_json(data, PLAN_FIELDS, "debug plan")
        artifacts = mapping["artifacts"]
        markers = mapping["markers"]
        if not isinstance(artifacts, list) or not isinstance(markers, list):
            raise DebugContractError("debug plan arrays have wrong types")
        plan = cls(
            schema_version=mapping["schema_version"],
            profile=mapping["profile"],
            artifacts=tuple(ArtifactIdentity.from_dict(item) for item in artifacts),
            bootargs=mapping["bootargs"],
            smp=mapping["smp"],
            sv39=mapping["sv39"],
            markers=tuple(markers),
            reboot_after=mapping["reboot_after"],
        )
        plan.validate()
        return plan


@dataclass(frozen=True)
class StageResult:
    """One canonical outcome bound to an exact debug plan."""

    schema_version: int
    stage: str
    passed: bool
    reason: str
    plan_sha256: str
    evidence: tuple[str, ...]

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise DebugContractError("stage result schema must be 1")
        if self.stage not in RESULT_STAGES:
            raise DebugContractError("unknown debug stage")
        if type(self.passed) is not bool:
            raise DebugContractError("stage passed field must be boolean")
        if not _safe_text(self.reason):
            raise DebugContractError("invalid stage reason")
        if (
            not isinstance(self.plan_sha256, str)
            or SHA256_PATTERN.fullmatch(self.plan_sha256) is None
        ):
            raise DebugContractError("invalid plan SHA-256")
        if (
            not isinstance(self.evidence, tuple)
            or len(set(self.evidence)) != len(self.evidence)
            or any(not _safe_text(item) for item in self.evidence)
        ):
            raise DebugContractError("invalid stage evidence")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "passed": self.passed,
            "reason": self.reason,
            "plan_sha256": self.plan_sha256,
            "evidence": list(self.evidence),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_bytes(cls, data: bytes) -> StageResult:
        mapping = _load_exact_json(data, RESULT_FIELDS, "stage result")
        evidence = mapping["evidence"]
        if not isinstance(evidence, list):
            raise DebugContractError("stage evidence must be an array")
        result = cls(
            schema_version=mapping["schema_version"],
            stage=mapping["stage"],
            passed=mapping["passed"],
            reason=mapping["reason"],
            plan_sha256=mapping["plan_sha256"],
            evidence=tuple(evidence),
        )
        result.validate()
        return result


def _safe_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode()) <= 1024
        and all(
            ord(character) >= 0x20 and ord(character) != 0x7F for character in value
        )
    )


def _canonical_json(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def _duplicate_rejecting_dict(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DebugContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_mapping(
    value: object, expected: frozenset[str], label: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise DebugContractError(f"{label} has unknown or missing fields")
    return value


def _load_exact_json(
    data: bytes, expected: frozenset[str], label: str
) -> dict[str, Any]:
    try:
        decoded = data.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_duplicate_rejecting_dict)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DebugContractError(f"{label} is not valid UTF-8 JSON") from error
    return _exact_mapping(value, expected, label)
