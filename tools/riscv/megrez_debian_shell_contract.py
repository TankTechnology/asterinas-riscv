#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Immutable identities for a persistent Debian shell on Megrez."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import stat
from typing import Any
import zlib

from tools.riscv.debian.rootfs.contract import (
    ContractError as RootfsContractError,
    RootfsManifest,
    load_manifest,
    load_package_checksums,
    validate_frozen_root,
)


P2_START_LBA = 0x000F_A022
P2_NR_SECTORS = 0x0080_0000
SHELL_ARTIFACT_ORDER = (
    "qemu_kernel",
    "megrez_kernel",
    "stage1",
    "installer_base",
    "qemu_uboot",
    "qemu_dtb",
    "megrez_dtb",
    "root_image",
    "root_manifest",
    "packages_lock",
    "package_checksums",
    "in_release",
)

_SCHEMA_VERSION = 1
_SMP = 4
_QEMU_PAGING = "sv39"
_MEGREZ_PAGING = "sv48"
_GATE_REBOOT_AFTER_SECONDS = 180
_LONG_OPERATION_REBOOT_AFTER_SECONDS = 600
_ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024
_MAX_BOOT_ARTIFACT_SIZE_BYTES = 64 * 1024 * 1024
_MAX_METADATA_SIZE_BYTES = 8 * 1024 * 1024
_HASH_CHUNK_SIZE_BYTES = 1024 * 1024
_METADATA_ARTIFACTS = {
    "root_manifest",
    "packages_lock",
    "package_checksums",
    "in_release",
}
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CRC32_RE = re.compile(r"\A[0-9a-f]{8}\Z")
_GIT_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_BOOT_TOKEN_RE = re.compile(r"\A[A-Za-z0-9._,/=:+-]+\Z")
_ARTIFACT_KEYS = {"name", "path", "size", "sha256", "crc32"}
_PLAN_KEYS = {
    "schema_version",
    "git_commit",
    "artifacts",
    "smp",
    "qemu_paging",
    "megrez_paging",
    "gate_bootargs",
    "final_bootargs",
    "gate_reboot_after",
    "long_operation_reboot_after",
    "partition_start_lba",
    "partition_nr_sectors",
}


class ShellContractError(ValueError):
    """A persistent-shell identity violates its frozen contract."""


@dataclass(frozen=True)
class FrozenArtifact:
    """The immutable content identity of one bundle artifact."""

    name: str
    path: str
    size: int
    sha256: str
    crc32: str

    @classmethod
    def from_path(cls, name: str, path: Path) -> FrozenArtifact:
        """Hashes one regular file through a single no-follow descriptor."""

        if not path.is_absolute():
            raise ShellContractError(f"{name} path must be absolute")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ShellContractError(f"{name} must be a regular file")
            digest = hashlib.sha256()
            crc = 0
            size = 0
            while chunk := os.read(descriptor, _HASH_CHUNK_SIZE_BYTES):
                size += len(chunk)
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
            if size != metadata.st_size:
                raise ShellContractError(f"{name} changed while reading")
            artifact = cls(
                name=name,
                path=str(path.absolute()),
                size=size,
                sha256=digest.hexdigest(),
                crc32=f"{crc:08x}",
            )
            artifact._validate_identity(metadata.st_size)
            return artifact
        finally:
            os.close(descriptor)

    def validate(self) -> None:
        """Validates artifact metadata without rereading its contents."""

        self._validate_identity(self.size)
        metadata = os.lstat(self.path)
        if stat.S_ISLNK(metadata.st_mode):
            raise ShellContractError(f"{self.name} path must not be a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise ShellContractError(f"{self.name} must be a regular file")
        if self.size != metadata.st_size:
            raise ShellContractError(f"{self.name} size does not match the file")

    def _validate_identity(self, file_size: int) -> None:
        if not isinstance(self.name, str) or self.name not in SHELL_ARTIFACT_ORDER:
            raise ShellContractError(f"unknown shell artifact: {self.name!r}")
        if not isinstance(self.path, str) or not Path(self.path).is_absolute():
            raise ShellContractError(f"{self.name} path must be absolute")
        _require_integer(self.size, f"{self.name} size")
        if self.size <= 0 or self.size != file_size:
            raise ShellContractError(f"{self.name} size does not match the file")
        if self.name == "root_image":
            if self.size != _ROOT_IMAGE_SIZE_BYTES:
                raise ShellContractError("root image must be exactly 1 GiB")
        elif self.name in _METADATA_ARTIFACTS:
            if self.size > _MAX_METADATA_SIZE_BYTES:
                raise ShellContractError(f"{self.name} exceeds the metadata size limit")
        elif self.size > _MAX_BOOT_ARTIFACT_SIZE_BYTES:
            raise ShellContractError(
                f"{self.name} exceeds the boot artifact size limit"
            )
        if (
            not isinstance(self.sha256, str)
            or _SHA256_RE.fullmatch(self.sha256) is None
        ):
            raise ShellContractError(
                f"{self.name} SHA-256 must be lowercase hexadecimal"
            )
        if not isinstance(self.crc32, str) or _CRC32_RE.fullmatch(self.crc32) is None:
            raise ShellContractError(f"{self.name} CRC32 must be lowercase hexadecimal")

    def to_mapping(self) -> dict[str, object]:
        """Returns the exact JSON representation of the artifact."""

        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "crc32": self.crc32,
        }


@dataclass(frozen=True)
class PersistentShellPlan:
    """The frozen dual-platform bundle used by QEMU and Megrez."""

    schema_version: int
    git_commit: str
    artifacts: tuple[FrozenArtifact, ...]
    smp: int
    qemu_paging: str
    megrez_paging: str
    gate_bootargs: str
    final_bootargs: str
    gate_reboot_after: int
    long_operation_reboot_after: int
    partition_start_lba: int
    partition_nr_sectors: int

    def validate(self) -> None:
        """Validates every invariant that is frozen into the bundle."""

        _require_exact_integer(self.schema_version, _SCHEMA_VERSION, "schema_version")
        if (
            not isinstance(self.git_commit, str)
            or _GIT_COMMIT_RE.fullmatch(self.git_commit) is None
        ):
            raise ShellContractError(
                "git_commit must be 40 lowercase hexadecimal digits"
            )
        if not isinstance(self.artifacts, tuple):
            raise ShellContractError("artifacts must be an immutable tuple")
        if any(not isinstance(artifact, FrozenArtifact) for artifact in self.artifacts):
            raise ShellContractError("artifacts must contain FrozenArtifact values")
        artifact_names = tuple(artifact.name for artifact in self.artifacts)
        if artifact_names != SHELL_ARTIFACT_ORDER:
            raise ShellContractError("artifacts do not match the frozen order")
        for artifact in self.artifacts:
            artifact.validate()
        _require_exact_integer(self.smp, _SMP, "smp")
        if self.qemu_paging != _QEMU_PAGING:
            raise ShellContractError("QEMU paging must be sv39")
        if self.megrez_paging != _MEGREZ_PAGING:
            raise ShellContractError("Megrez paging must be sv48")
        _validate_bootargs(self.gate_bootargs, "gate_bootargs")
        _validate_bootargs(self.final_bootargs, "final_bootargs")
        _require_exact_integer(
            self.gate_reboot_after,
            _GATE_REBOOT_AFTER_SECONDS,
            "gate_reboot_after",
        )
        _require_exact_integer(
            self.long_operation_reboot_after,
            _LONG_OPERATION_REBOOT_AFTER_SECONDS,
            "long_operation_reboot_after",
        )
        gate_tokens = self.gate_bootargs.split(" ")
        recovery_token = f"asterinas.reboot_after={_GATE_REBOOT_AFTER_SECONDS}"
        if gate_tokens.count(recovery_token) != 1 or any(
            token.startswith("asterinas.reboot_after=") and token != recovery_token
            for token in gate_tokens
        ):
            raise ShellContractError("gate_bootargs require one exact recovery token")
        final_tokens = self.final_bootargs.split(" ")
        if any(token.startswith("asterinas.reboot_after") for token in final_tokens):
            raise ShellContractError("final_bootargs must not arm recovery reboot")
        if "asterinas.mmc_write_partition2" in final_tokens:
            raise ShellContractError("final_bootargs must not permit partition writes")
        _require_exact_integer(
            self.partition_start_lba,
            P2_START_LBA,
            "partition_start_lba",
        )
        _require_exact_integer(
            self.partition_nr_sectors,
            P2_NR_SECTORS,
            "partition_nr_sectors",
        )

    def artifact_map(self) -> dict[str, FrozenArtifact]:
        """Returns artifacts indexed by their frozen names."""

        return {artifact.name: artifact for artifact in self.artifacts}

    @property
    def plan_sha256(self) -> str:
        """Returns the SHA-256 identity of the canonical plan."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def canonical_bytes(self) -> bytes:
        """Serializes the validated plan as canonical JSON."""

        self.validate()
        document = {
            "schema_version": self.schema_version,
            "git_commit": self.git_commit,
            "artifacts": [artifact.to_mapping() for artifact in self.artifacts],
            "smp": self.smp,
            "qemu_paging": self.qemu_paging,
            "megrez_paging": self.megrez_paging,
            "gate_bootargs": self.gate_bootargs,
            "final_bootargs": self.final_bootargs,
            "gate_reboot_after": self.gate_reboot_after,
            "long_operation_reboot_after": self.long_operation_reboot_after,
            "partition_start_lba": self.partition_start_lba,
            "partition_nr_sectors": self.partition_nr_sectors,
        }
        return (
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> PersistentShellPlan:
        """Parses an exact bundle document with duplicate-key rejection."""

        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ShellContractError("shell plan must be UTF-8") from error
        try:
            raw = json.loads(text, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as error:
            raise ShellContractError(
                f"invalid shell plan JSON at line {error.lineno}, column {error.colno}"
            ) from error
        document = _mapping(raw, "shell plan")
        _exact_keys(document, _PLAN_KEYS, "shell plan")
        artifact_values = document["artifacts"]
        if not isinstance(artifact_values, list):
            raise ShellContractError("artifacts must be an array")
        artifacts = []
        for index, value in enumerate(artifact_values):
            artifact = _mapping(value, f"artifact {index}")
            _exact_keys(artifact, _ARTIFACT_KEYS, f"artifact {index}")
            artifacts.append(
                FrozenArtifact(
                    name=_string(artifact["name"], f"artifact {index} name"),
                    path=_string(artifact["path"], f"artifact {index} path"),
                    size=_integer(artifact["size"], f"artifact {index} size"),
                    sha256=_string(artifact["sha256"], f"artifact {index} SHA-256"),
                    crc32=_string(artifact["crc32"], f"artifact {index} CRC32"),
                )
            )
        plan = cls(
            schema_version=_integer(document["schema_version"], "schema_version"),
            git_commit=_string(document["git_commit"], "git_commit"),
            artifacts=tuple(artifacts),
            smp=_integer(document["smp"], "smp"),
            qemu_paging=_string(document["qemu_paging"], "qemu_paging"),
            megrez_paging=_string(document["megrez_paging"], "megrez_paging"),
            gate_bootargs=_string(document["gate_bootargs"], "gate_bootargs"),
            final_bootargs=_string(document["final_bootargs"], "final_bootargs"),
            gate_reboot_after=_integer(
                document["gate_reboot_after"], "gate_reboot_after"
            ),
            long_operation_reboot_after=_integer(
                document["long_operation_reboot_after"],
                "long_operation_reboot_after",
            ),
            partition_start_lba=_integer(
                document["partition_start_lba"], "partition_start_lba"
            ),
            partition_nr_sectors=_integer(
                document["partition_nr_sectors"], "partition_nr_sectors"
            ),
        )
        plan.validate()
        if plan.canonical_bytes() != payload:
            raise ShellContractError("shell plan must use canonical JSON")
        return plan


def validate_rootfs_identity(plan: PersistentShellPlan) -> RootfsManifest:
    """Binds the bundle to the complete signed rootfs identity."""

    plan.validate()
    artifacts = plan.artifact_map()
    try:
        manifest = load_manifest(Path(artifacts["root_manifest"].path))
        validated = validate_frozen_root(
            Path(artifacts["root_image"].path),
            manifest,
            Path(artifacts["packages_lock"].path),
        )
        checksums = load_package_checksums(Path(artifacts["package_checksums"].path))
    except RootfsContractError as error:
        raise ShellContractError(f"rootfs identity is invalid: {error}") from error
    if validated.schema_version != 1 or validated.profile != "minimal-m1":
        raise ShellContractError("persistent shell requires the minimal-m1 root")
    if checksums != validated.downloaded_packages:
        raise ShellContractError("package checksums do not match the manifest")
    if not hmac.compare_digest(
        validated.root_image_sha256, artifacts["root_image"].sha256
    ):
        raise ShellContractError("root image identity differs from the bundle")
    if not hmac.compare_digest(
        validated.signed_metadata_sha256, artifacts["in_release"].sha256
    ):
        raise ShellContractError("retained InRelease differs from the manifest")
    return validated


def _validate_bootargs(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ShellContractError(f"{field} must be a non-empty canonical string")
    tokens = value.split(" ")
    if any(not token or _BOOT_TOKEN_RE.fullmatch(token) is None for token in tokens):
        raise ShellContractError(f"{field} contains unsafe characters")


def _require_integer(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShellContractError(f"{field} must be an integer")


def _require_exact_integer(value: object, expected: int, field: str) -> None:
    _require_integer(value, field)
    if value != expected:
        raise ShellContractError(f"{field} must be {expected}")


def _integer(value: object, field: str) -> int:
    _require_integer(value, field)
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ShellContractError(f"{field} must be a string")
    return value


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ShellContractError(f"{field} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    keys = set(value)
    if keys != expected:
        missing = sorted(expected - keys)
        unknown = sorted(keys - expected)
        raise ShellContractError(
            f"{field} fields differ: missing={missing}, unknown={unknown}"
        )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ShellContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value
