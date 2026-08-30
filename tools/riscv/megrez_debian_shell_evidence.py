#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validated evidence for the Megrez persistent Debian shell workflow."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from tools.riscv.debian.rootfs.gate_protocol import GENERIC_SV39_CPU
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.debian.rootfs.rootfs_gate import verify_four_hart_dtb
from tools.riscv.megrez_debian_shell_contract import (
    SHELL_ARTIFACT_ORDER,
    FrozenArtifact,
    PersistentShellPlan,
    ShellContractError,
    validate_rootfs_identity,
)


_MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CRC32_RE = re.compile(r"\A[0-9a-f]{8}\Z")
_GIT_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_BOOT_TOKEN_RE = re.compile(r"\A[A-Za-z0-9._,/=:+-]+\Z")
_QEMU_EVIDENCE_KEYS = {
    "schema_version",
    "passed",
    "reason",
    "plan_sha256",
    "native_result_sha256",
    "boot_count",
    "qemu_kernel_sha256",
    "root_image_sha256",
}
_PERMIT_KEYS = {
    "schema_version",
    "passed",
    "reason",
    "plan_sha256",
    "qemu_evidence_sha256",
    "git_commit",
    "megrez_kernel_sha256",
    "stage1_crc32",
    "megrez_dtb_crc32",
    "root_image_sha256",
    "gate_bootargs",
    "gate_reboot_after",
    "long_operation_reboot_after",
}
_NATIVE_RESULT_KEYS = {
    "passed",
    "reason",
    "nonce_sha256",
    "qemu_argv",
    "input_sha256",
    "final_root_sha256",
    "manifest_identity",
    "package_identity",
    "phase_durations_seconds",
}
_NATIVE_INPUT_NAMES = {
    "kernel": "qemu_kernel",
    "u_boot": "qemu_uboot",
    "dtb": "qemu_dtb",
    "stage1_initramfs": "stage1",
    "root_image": "root_image",
    "manifest": "root_manifest",
    "packages_lock": "packages_lock",
    "package_checksums": "package_checksums",
}


class ShellPermitError(ValueError):
    """QEMU evidence or a physical-board permit is invalid."""


@dataclass(frozen=True)
class QemuShellEvidence:
    """The immutable result of a successful generic-Sv39 QEMU gate."""

    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    native_result_sha256: str
    boot_count: int
    qemu_kernel_sha256: str
    root_image_sha256: str

    def validate(self) -> None:
        _exact_integer(self.schema_version, 1, "schema_version")
        if self.passed is not True or self.reason != "pass":
            raise ShellPermitError("QEMU evidence must record an exact pass")
        _sha256(self.plan_sha256, "plan_sha256")
        _sha256(self.native_result_sha256, "native_result_sha256")
        _exact_integer(self.boot_count, 2, "boot_count")
        _sha256(self.qemu_kernel_sha256, "qemu_kernel_sha256")
        _sha256(self.root_image_sha256, "root_image_sha256")

    def canonical_bytes(self) -> bytes:
        self.validate()
        return _canonical_bytes(
            {
                "schema_version": self.schema_version,
                "passed": self.passed,
                "reason": self.reason,
                "plan_sha256": self.plan_sha256,
                "native_result_sha256": self.native_result_sha256,
                "boot_count": self.boot_count,
                "qemu_kernel_sha256": self.qemu_kernel_sha256,
                "root_image_sha256": self.root_image_sha256,
            }
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> QemuShellEvidence:
        document = _load_exact_json(payload, _QEMU_EVIDENCE_KEYS, "QEMU evidence")
        evidence = cls(**document)
        evidence.validate()
        if evidence.canonical_bytes() != payload:
            raise ShellPermitError("QEMU evidence must use canonical JSON")
        return evidence


@dataclass(frozen=True)
class ShellPermit:
    """The immutable authorization to begin physical-board inspection."""

    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    qemu_evidence_sha256: str
    git_commit: str
    megrez_kernel_sha256: str
    stage1_crc32: str
    megrez_dtb_crc32: str
    root_image_sha256: str
    gate_bootargs: str
    gate_reboot_after: int
    long_operation_reboot_after: int

    def validate(self) -> None:
        _exact_integer(self.schema_version, 1, "schema_version")
        if self.passed is not True or self.reason != "pass":
            raise ShellPermitError("shell permit must record an exact pass")
        for field, value in (
            ("plan_sha256", self.plan_sha256),
            ("qemu_evidence_sha256", self.qemu_evidence_sha256),
            ("megrez_kernel_sha256", self.megrez_kernel_sha256),
            ("root_image_sha256", self.root_image_sha256),
        ):
            _sha256(value, field)
        if (
            not isinstance(self.git_commit, str)
            or _GIT_COMMIT_RE.fullmatch(self.git_commit) is None
        ):
            raise ShellPermitError("git_commit must be 40 lowercase hexadecimal digits")
        for field, value in (
            ("stage1_crc32", self.stage1_crc32),
            ("megrez_dtb_crc32", self.megrez_dtb_crc32),
        ):
            if not isinstance(value, str) or _CRC32_RE.fullmatch(value) is None:
                raise ShellPermitError(f"{field} must be lowercase hexadecimal")
        if (
            not isinstance(self.gate_bootargs, str)
            or self.gate_bootargs != self.gate_bootargs.strip()
            or any(
                not token or _BOOT_TOKEN_RE.fullmatch(token) is None
                for token in self.gate_bootargs.split(" ")
            )
        ):
            raise ShellPermitError("gate_bootargs must be a safe canonical string")
        if self.gate_bootargs.split(" ").count("asterinas.reboot_after=180") != 1:
            raise ShellPermitError("gate_bootargs require one exact recovery token")
        _exact_integer(self.gate_reboot_after, 180, "gate_reboot_after")
        _exact_integer(
            self.long_operation_reboot_after,
            600,
            "long_operation_reboot_after",
        )

    def canonical_bytes(self) -> bytes:
        self.validate()
        return _canonical_bytes(
            {
                "schema_version": self.schema_version,
                "passed": self.passed,
                "reason": self.reason,
                "plan_sha256": self.plan_sha256,
                "qemu_evidence_sha256": self.qemu_evidence_sha256,
                "git_commit": self.git_commit,
                "megrez_kernel_sha256": self.megrez_kernel_sha256,
                "stage1_crc32": self.stage1_crc32,
                "megrez_dtb_crc32": self.megrez_dtb_crc32,
                "root_image_sha256": self.root_image_sha256,
                "gate_bootargs": self.gate_bootargs,
                "gate_reboot_after": self.gate_reboot_after,
                "long_operation_reboot_after": self.long_operation_reboot_after,
            }
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> ShellPermit:
        document = _load_exact_json(payload, _PERMIT_KEYS, "shell permit")
        permit = cls(**document)
        permit.validate()
        if permit.canonical_bytes() != payload:
            raise ShellPermitError("shell permit must use canonical JSON")
        return permit


def validate_qemu_result(
    plan: PersistentShellPlan, result_path: Path
) -> QemuShellEvidence:
    """Validates one complete native two-boot result and both serial logs."""

    plan.validate()
    payload = _read_regular(result_path, "native result")
    document = _load_exact_json(payload, _NATIVE_RESULT_KEYS, "native result")
    if document["passed"] is not True or document["reason"] != "pass":
        raise ShellPermitError("native QEMU gate did not pass")
    _sha256(document["nonce_sha256"], "nonce_sha256")
    input_hashes = _mapping(document["input_sha256"], "input_sha256")
    if set(input_hashes) != set(_NATIVE_INPUT_NAMES):
        raise ShellPermitError("native QEMU inputs have unexpected fields")
    artifacts = plan.artifact_map()
    for native_name, artifact_name in _NATIVE_INPUT_NAMES.items():
        expected = artifacts[artifact_name].sha256
        actual = input_hashes[native_name]
        _sha256(actual, f"input_sha256.{native_name}")
        if not hmac.compare_digest(actual, expected):
            raise ShellPermitError(f"native QEMU input differs: {native_name}")
    argv_values = document["qemu_argv"]
    if not isinstance(argv_values, list) or len(argv_values) != 2:
        raise ShellPermitError("native QEMU gate must contain exactly two boots")
    for argv in argv_values:
        _validate_qemu_argv(argv)
    _sha256(document["final_root_sha256"], "final_root_sha256")
    manifest = _mapping(document["manifest_identity"], "manifest_identity")
    if (
        manifest.get("suite") != "trixie"
        or manifest.get("architecture") != "riscv64"
        or manifest.get("root_image_sha256") != artifacts["root_image"].sha256
    ):
        raise ShellPermitError("native QEMU manifest identity differs")
    for name in ("boot1.serial.log", "boot2.serial.log"):
        if not _read_regular(result_path.parent / name, name):
            raise ShellPermitError(f"{name} must not be empty")
    return QemuShellEvidence(
        schema_version=1,
        passed=True,
        reason="pass",
        plan_sha256=plan.plan_sha256,
        native_result_sha256=hashlib.sha256(payload).hexdigest(),
        boot_count=2,
        qemu_kernel_sha256=artifacts["qemu_kernel"].sha256,
        root_image_sha256=artifacts["root_image"].sha256,
    )


def issue_shell_permit(
    plan: PersistentShellPlan,
    qemu_evidence_path: Path,
    permit_path: Path,
    *,
    repository: Path,
    artifact_reader: Callable[[str, Path], FrozenArtifact] = FrozenArtifact.from_path,
    rootfs_validator: Callable[
        [PersistentShellPlan], object
    ] = validate_rootfs_identity,
    dtb_validator: Callable[[Path], int] = verify_four_hart_dtb,
    git_identity: Callable[[Path], str] | None = None,
) -> ShellPermit:
    """Revalidates every prerequisite and atomically publishes a board permit."""

    git_identity = git_identity or _clean_git_identity
    if not permit_path.is_absolute() or not qemu_evidence_path.is_absolute():
        raise ShellPermitError("evidence and permit paths must be absolute")
    if permit_path.is_symlink():
        raise ShellPermitError("permit output must not be a symbolic link")
    try:
        with PinnedOutputDirectory(permit_path.parent) as output:
            output.invalidate(permit_path.name)
            plan.validate()
            evidence_payload = _read_regular(qemu_evidence_path, "QEMU evidence")
            evidence = QemuShellEvidence.from_bytes(evidence_payload)
            if evidence.plan_sha256 != plan.plan_sha256:
                raise ShellPermitError("QEMU evidence belongs to a different plan")
            artifacts = plan.artifact_map()
            if (
                evidence.qemu_kernel_sha256 != artifacts["qemu_kernel"].sha256
                or evidence.root_image_sha256 != artifacts["root_image"].sha256
            ):
                raise ShellPermitError("QEMU evidence has stale artifact identities")
            for name in SHELL_ARTIFACT_ORDER:
                expected = artifacts[name]
                actual = artifact_reader(name, Path(expected.path))
                if actual != expected:
                    raise ShellPermitError(f"bundle artifact changed: {name}")
            rootfs_validator(plan)
            for name in ("qemu_dtb", "megrez_dtb"):
                if dtb_validator(Path(artifacts[name].path)) != 4:
                    raise ShellPermitError(f"{name} must expose exactly four CPUs")
            current_commit = git_identity(repository)
            if current_commit != plan.git_commit:
                raise ShellPermitError("working tree is dirty or on another commit")
            permit = ShellPermit(
                schema_version=1,
                passed=True,
                reason="pass",
                plan_sha256=plan.plan_sha256,
                qemu_evidence_sha256=hashlib.sha256(evidence_payload).hexdigest(),
                git_commit=plan.git_commit,
                megrez_kernel_sha256=artifacts["megrez_kernel"].sha256,
                stage1_crc32=artifacts["stage1"].crc32,
                megrez_dtb_crc32=artifacts["megrez_dtb"].crc32,
                root_image_sha256=artifacts["root_image"].sha256,
                gate_bootargs=plan.gate_bootargs,
                gate_reboot_after=plan.gate_reboot_after,
                long_operation_reboot_after=plan.long_operation_reboot_after,
            )
            output.atomic_write(permit_path.name, permit.canonical_bytes())
            return permit
    except ShellPermitError:
        raise
    except (OSError, ShellContractError, subprocess.SubprocessError) as error:
        raise ShellPermitError(f"failed to issue shell permit: {error}") from error


def _validate_qemu_argv(value: object) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(argument, str) for argument in value)
    ):
        raise ShellPermitError("QEMU argv must be a non-empty string array")
    if value[0] != "qemu-system-riscv64":
        raise ShellPermitError("QEMU executable differs from the frozen contract")
    for option, expected in (
        ("-machine", "virt"),
        ("-cpu", GENERIC_SV39_CPU),
        ("-m", "2G"),
        ("-smp", "4"),
        ("-display", "none"),
        ("-nic", "none"),
        ("-serial", "stdio"),
    ):
        if value.count(option) != 1:
            raise ShellPermitError(f"QEMU argv requires one {option}")
        index = value.index(option)
        if index + 1 >= len(value) or value[index + 1] != expected:
            raise ShellPermitError(f"QEMU {option} differs from the frozen contract")
    option_sequence = [argument for argument in value[1:] if argument.startswith("-")]
    expected_options = [
        "-machine",
        "-cpu",
        "-m",
        "-smp",
        "-display",
        "-nic",
        "-serial",
        "-no-reboot",
        "-kernel",
        "-drive",
        "-device",
        "-drive",
        "-device",
        "-monitor",
    ]
    device_values = [
        value[index + 1]
        for index, argument in enumerate(value[:-1])
        if argument == "-device"
    ]
    if option_sequence != expected_options or device_values != [
        "virtio-blk-device,drive=bootdisk",
        "virtio-blk-device,drive=rootdisk",
    ]:
        raise ShellPermitError("QEMU argv contains unexpected options or devices")


def _clean_git_identity(repository: Path) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if dirty:
        raise ShellPermitError("working tree must be clean")
    return commit


def _read_regular(path: Path, role: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ShellPermitError(f"{role} must be a no-follow regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_EVIDENCE_BYTES:
            raise ShellPermitError(f"{role} must be a bounded regular file")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > _MAX_EVIDENCE_BYTES:
                raise ShellPermitError(f"{role} exceeds 8 MiB")
            chunks.append(chunk)
        if size != metadata.st_size:
            raise ShellPermitError(f"{role} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_exact_json(payload: bytes, keys: set[str], role: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ShellPermitError(f"{role} must be UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError as error:
        raise ShellPermitError(f"invalid {role} JSON") from error
    document = _mapping(value, role)
    if set(document) != keys:
        raise ShellPermitError(f"{role} fields differ from the exact contract")
    return document


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShellPermitError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _mapping(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ShellPermitError(f"{role} must be an object")
    return value


def _sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ShellPermitError(f"{field} must be lowercase hexadecimal")


def _exact_integer(value: object, expected: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise ShellPermitError(f"{field} must be {expected}")
