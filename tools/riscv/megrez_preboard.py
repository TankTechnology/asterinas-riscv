#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Issue one immutable permit for a controlled Megrez Debian board boot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.riscv.debian.rootfs.contract import (
    load_manifest,
    load_package_checksums,
    validate_frozen_root,
)
from tools.riscv.debian.rootfs.gate_protocol import GENERIC_SV39_CPU
from tools.riscv.debian.rootfs.rootfs_gate import verify_four_hart_dtb
from tools.riscv.megrez_debug_contract import (
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_simulation import _validate_current_artifacts

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TRANSFER_NAMES = ("kernel", "initramfs", "megrez_dtb")
_RECOVERY_BOOTARGS = (
    "console=ttyS0 loglevel=info init=/init "
    "asterinas.net=eic7700-rj45,10.100.19.200/21 "
    "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e "
    "asterinas.reboot_after=60"
)
_RECOVERY_TRIGGER = (
    b"ASTERINAS_GMAC_TCP_PROBE_READY peer=10.100.19.216:18080 "
    b"status=200 sizes=16384,65536,1048576,16777216 "
    b"completed_bytes=17907712 pattern=mod251"
)
_RECOVERY_FIELDS = frozenset(
    (
        "schema_version",
        "passed",
        "reason",
        "plan_sha256",
        "kernel_sha256",
        "native_result_sha256",
        "serial_sha256",
        "second_firmware_epoch",
        "fresh_uboot_prompt",
    )
)
_PERMIT_FIELDS = frozenset(
    (
        "schema_version",
        "passed",
        "reason",
        "plan_sha256",
        "desktop_result_sha256",
        "recovery_result_sha256",
        "git_commit",
        "kernel_sha256",
        "transfer_crc32",
        "bootargs",
        "reboot_after",
    )
)
ArtifactValidator = Callable[[DebugPlan], dict[str, ArtifactIdentity]]
RootfsValidator = Callable[[dict[str, ArtifactIdentity]], None]
DtbValidator = Callable[[Path], None]
GitIdentity = Callable[[Path], str]


class PreboardError(RuntimeError):
    """One failure that forbids opening the physical board."""


def _exact_mapping(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise PreboardError(f"invalid {label} fields")
    return value


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PreboardError(f"duplicate {label} key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreboardError(f"malformed {label} JSON") from error
    if not isinstance(value, dict):
        raise PreboardError(f"invalid {label} document")
    return value


def _canonical_bytes(document: dict[str, object]) -> bytes:
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _read_held(path: Path, *, label: str) -> tuple[bytes, str]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PreboardError(f"{label}-missing: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= MAX_EVIDENCE_BYTES
        ):
            raise PreboardError(f"{label}-invalid")
        digest = hashlib.sha256()
        payload = bytearray()
        while len(payload) <= MAX_EVIDENCE_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_EVIDENCE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
            digest.update(chunk)
        if len(payload) != metadata.st_size:
            raise PreboardError(f"{label}-size-changed")
        return bytes(payload), digest.hexdigest()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class RecoveryEvidence:
    """Plan-bound proof of one fresh QEMU firmware epoch."""

    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    kernel_sha256: str
    native_result_sha256: str
    serial_sha256: str
    second_firmware_epoch: bool
    fresh_uboot_prompt: bool

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.passed is not True
            or self.reason != "recovery-pass"
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in (
                    self.plan_sha256,
                    self.kernel_sha256,
                    self.native_result_sha256,
                    self.serial_sha256,
                )
            )
            or self.second_firmware_epoch is not True
            or self.fresh_uboot_prompt is not True
        ):
            raise PreboardError("invalid recovery evidence")

    def canonical_bytes(self) -> bytes:
        self.validate()
        return _canonical_bytes(
            {
                "schema_version": self.schema_version,
                "passed": self.passed,
                "reason": self.reason,
                "plan_sha256": self.plan_sha256,
                "kernel_sha256": self.kernel_sha256,
                "native_result_sha256": self.native_result_sha256,
                "serial_sha256": self.serial_sha256,
                "second_firmware_epoch": self.second_firmware_epoch,
                "fresh_uboot_prompt": self.fresh_uboot_prompt,
            }
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> RecoveryEvidence:
        value = _exact_mapping(
            _decode_json(payload, label="recovery"), _RECOVERY_FIELDS, "recovery"
        )
        result = cls(**value)
        result.validate()
        return result


@dataclass(frozen=True)
class PreboardPermit:
    """The sole immutable authorization for one physical desktop attempt."""

    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    desktop_result_sha256: str
    recovery_result_sha256: str
    git_commit: str
    kernel_sha256: str
    transfer_crc32: tuple[tuple[str, str], ...]
    bootargs: str
    reboot_after: int

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.passed is not True
            or self.reason != "preboard-pass"
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in (
                    self.plan_sha256,
                    self.desktop_result_sha256,
                    self.recovery_result_sha256,
                    self.kernel_sha256,
                )
            )
            or not isinstance(self.git_commit, str)
            or _COMMIT.fullmatch(self.git_commit) is None
            or tuple(name for name, _crc in self.transfer_crc32) != _TRANSFER_NAMES
            or any(
                not isinstance(crc, str) or re.fullmatch(r"[0-9a-f]{8}", crc) is None
                for _name, crc in self.transfer_crc32
            )
            or not isinstance(self.bootargs, str)
            or type(self.reboot_after) is not int
            or self.reboot_after <= 0
            or f"asterinas.reboot_after={self.reboot_after}"
            not in self.bootargs.split()
        ):
            raise PreboardError("invalid preboard permit")

    def canonical_bytes(self) -> bytes:
        self.validate()
        return _canonical_bytes(
            {
                "schema_version": self.schema_version,
                "passed": self.passed,
                "reason": self.reason,
                "plan_sha256": self.plan_sha256,
                "desktop_result_sha256": self.desktop_result_sha256,
                "recovery_result_sha256": self.recovery_result_sha256,
                "git_commit": self.git_commit,
                "kernel_sha256": self.kernel_sha256,
                "transfer_crc32": [
                    {"name": name, "crc32": crc} for name, crc in self.transfer_crc32
                ],
                "bootargs": self.bootargs,
                "reboot_after": self.reboot_after,
            }
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> PreboardPermit:
        value = _exact_mapping(
            _decode_json(payload, label="permit"), _PERMIT_FIELDS, "permit"
        )
        transfers = value["transfer_crc32"]
        if not isinstance(transfers, list):
            raise PreboardError("invalid permit transfer list")
        parsed: list[tuple[str, str]] = []
        for transfer in transfers:
            item = _exact_mapping(
                transfer, frozenset(("name", "crc32")), "permit transfer"
            )
            parsed.append((item["name"], item["crc32"]))
        result = cls(
            schema_version=value["schema_version"],
            passed=value["passed"],
            reason=value["reason"],
            plan_sha256=value["plan_sha256"],
            desktop_result_sha256=value["desktop_result_sha256"],
            recovery_result_sha256=value["recovery_result_sha256"],
            git_commit=value["git_commit"],
            kernel_sha256=value["kernel_sha256"],
            transfer_crc32=tuple(parsed),
            bootargs=value["bootargs"],
            reboot_after=value["reboot_after"],
        )
        result.validate()
        return result


def _argument_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    values = arguments[index + 1 : index + 2]
    return values[0] if values else None


def _validate_native_recovery(native: dict[str, Any], kernel: ArtifactIdentity) -> None:
    artifacts = native.get("artifacts")
    audit = native.get("audit")
    session = native.get("session")
    arguments = native.get("qemu_argv")
    if (
        not isinstance(artifacts, dict)
        or artifacts.get("kernel_crc32") != kernel.crc32
        or artifacts.get("kernel_size") != kernel.size
        or not isinstance(audit, dict)
        or audit.get("passed") is not True
        or audit.get("failures") != []
        or audit.get("booti_command_count") != 1
        or native.get("boot_disk_sha256_before")
        != native.get("boot_disk_sha256_after")
        or not isinstance(native.get("boot_disk_sha256_before"), str)
        or _SHA256.fullmatch(native["boot_disk_sha256_before"]) is None
        or native.get("passed") is not True
        or native.get("profile") != "generic-sv39-smp4-software-reboot"
        or native.get("validation_scenario") != "megrez-tcp-probe-recovery"
        or native.get("scenario") != "positive"
        or native.get("effective_bootargs") != _RECOVERY_BOOTARGS
        or not isinstance(session, dict)
        or session.get("booti_sent_count") != 1
        or session.get("recovery_complete") is not True
        or session.get("cleanup_complete") is not True
        or session.get("failure") is not None
        or session.get("timed_out") is not False
        or not isinstance(arguments, list)
        or not all(isinstance(argument, str) for argument in arguments)
        or _argument_value(arguments, "-cpu") != GENERIC_SV39_CPU
        or _argument_value(arguments, "-m") != "2G"
        or _argument_value(arguments, "-smp") != "4"
        or _argument_value(arguments, "-display") != "none"
        or _argument_value(arguments, "-monitor") != "none"
        or "-enable-kvm" in arguments
        or "-accel" in arguments
        or "-no-reboot" in arguments
    ):
        raise PreboardError("native recovery contract mismatch")


def _kernel_hash_from_sums(payload: bytes) -> str:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise PreboardError("malformed recovery SHA256SUMS") from error
    matches: list[str] = []
    for line in lines:
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and Path(fields[1].lstrip(" *")).name == "asterinas.booti":
            matches.append(fields[0])
    if len(matches) != 1 or _SHA256.fullmatch(matches[0]) is None:
        raise PreboardError("recovery kernel SHA-256 is missing or ambiguous")
    return matches[0]


def _validate_recovery_transcript(transcript: bytes) -> None:
    armed = transcript.find(b"ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=60")
    trigger = transcript.find(_RECOVERY_TRIGGER, armed + 1)
    opensbi = transcript.find(b"OpenSBI v", trigger + 1)
    uboot = transcript.find(b"U-Boot 2026.07", opensbi + 1)
    prompt = transcript.find(b"=> ", uboot + 1)
    if min(armed, trigger, opensbi, uboot, prompt) < 0 or not (
        armed < trigger < opensbi < uboot < prompt
    ):
        raise PreboardError("recovery transcript lacks a fresh firmware epoch")


def create_recovery_evidence(
    plan: DebugPlan,
    native_result: Path,
    serial_log: Path,
    sha256sums: Path,
) -> RecoveryEvidence:
    """Translate the dedicated QEMU reboot gate into plan-bound evidence."""

    try:
        plan.validate()
    except Exception as error:
        raise PreboardError(f"recovery plan invalid: {error}") from error
    if (plan.schema_version, plan.profile) not in (
        (1, "tcp-probe"),
        (2, "debian-browser"),
    ):
        raise PreboardError("recovery requires a Megrez probe or browser plan")
    identities = {identity.name: identity for identity in plan.artifacts}
    native_payload, native_hash = _read_held(native_result, label="recovery-result")
    serial_payload, serial_hash = _read_held(serial_log, label="recovery-serial")
    sums_payload, _sums_hash = _read_held(sha256sums, label="recovery-sums")
    native = _decode_json(native_payload, label="native recovery")
    _validate_native_recovery(native, identities["kernel"])
    if _kernel_hash_from_sums(sums_payload) != identities["kernel"].sha256:
        raise PreboardError("recovery kernel does not match the plan")
    _validate_recovery_transcript(serial_payload)
    result = RecoveryEvidence(
        1,
        True,
        "recovery-pass",
        plan.plan_sha256,
        identities["kernel"].sha256,
        native_hash,
        serial_hash,
        True,
        True,
    )
    result.validate()
    return result


def _validate_rootfs(identities: dict[str, ArtifactIdentity]) -> None:
    manifest = load_manifest(Path(identities["root_manifest"].path))
    manifest = validate_frozen_root(
        Path(identities["root_image"].path),
        manifest,
        Path(identities["packages_lock"].path),
    )
    checksums = load_package_checksums(Path(identities["package_checksums"].path))
    if checksums != manifest.downloaded_packages:
        raise PreboardError("preboard package checksums mismatch")


def _git_identity(repository: Path) -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise PreboardError(
            f"cannot identify the current Git commit: {error}"
        ) from error
    if _COMMIT.fullmatch(commit) is None or status:
        raise PreboardError("preboard requires one clean committed worktree")
    return commit


class _PinnedPermitOutput:
    """Hold the permit parent across validation and descriptor-relative publish."""

    def __init__(self, path: Path, repository: Path) -> None:
        self.path = path.absolute()
        try:
            self.path.relative_to(repository / "target")
        except ValueError as error:
            raise PreboardError("preboard-output-outside-target") from error
        parent = self.path.parent
        current = Path(parent.anchor)
        for part in parent.parts[1:]:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise PreboardError("preboard-output-parent-unsafe")
        parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.descriptor = os.open(parent, flags)
        except OSError as error:
            raise PreboardError(f"preboard-output-parent-unsafe: {error}") from error
        if not os.path.samestat(parent.lstat(), os.fstat(self.descriptor)):
            self.close()
            raise PreboardError("preboard-output-parent-changed")

    def __enter__(self) -> _PinnedPermitOutput:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def _check_parent(self) -> None:
        try:
            current = self.path.parent.lstat()
        except OSError as error:
            raise PreboardError("preboard-output-parent-changed") from error
        if not os.path.samestat(current, os.fstat(self.descriptor)):
            raise PreboardError("preboard-output-parent-changed")

    def invalidate(self) -> None:
        try:
            metadata = os.stat(
                self.path.name, dir_fd=self.descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PreboardError("preboard-output-unsafe")
        os.unlink(self.path.name, dir_fd=self.descriptor)
        os.fsync(self.descriptor)

    def write(self, payload: bytes) -> None:
        temporary_name = f".preboard.tmp.{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=self.descriptor)
        try:
            os.fchmod(descriptor, 0o644)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise PreboardError("preboard-output-short-write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            self._check_parent()
            try:
                os.stat(self.path.name, dir_fd=self.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PreboardError("preboard-output-reappeared")
            os.replace(
                temporary_name,
                self.path.name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
            os.fsync(self.descriptor)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass


def issue_preboard_permit(
    plan: DebugPlan,
    desktop_result: Path,
    recovery_result: Path,
    output: Path,
    *,
    artifact_validator: ArtifactValidator = _validate_current_artifacts,
    rootfs_validator: RootfsValidator = _validate_rootfs,
    dtb_validator: DtbValidator = verify_four_hart_dtb,
    git_identity: GitIdentity = _git_identity,
    repository_root: Path | None = None,
) -> PreboardPermit:
    """Publish a permit only after every simulation and identity check passes."""

    repository = (
        repository_root.absolute()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    with _PinnedPermitOutput(output, repository) as publication:
        publication.invalidate()
        try:
            plan.validate()
            if plan.schema_version != 2 or plan.profile != "debian-browser":
                raise PreboardError("preboard requires a Debian browser plan")
            identities = artifact_validator(plan)
            rootfs_validator(identities)
            for name in ("qemu_dtb", "megrez_dtb"):
                dtb_validator(Path(identities[name].path))
            if artifact_validator(plan) != identities:
                raise PreboardError("preboard artifacts changed during validation")
            desktop_payload, desktop_hash = _read_held(
                desktop_result, label="desktop-result"
            )
            recovery_payload, recovery_hash = _read_held(
                recovery_result, label="recovery-result"
            )
            desktop = StageResult.from_bytes(desktop_payload)
            recovery = RecoveryEvidence.from_bytes(recovery_payload)
            if (
                desktop.stage != "desktop"
                or desktop.passed is not True
                or desktop.reason != "desktop-pass"
                or desktop.plan_sha256 != plan.plan_sha256
            ):
                raise PreboardError("desktop result does not match the plan")
            if (
                recovery.plan_sha256 != plan.plan_sha256
                or recovery.kernel_sha256 != identities["kernel"].sha256
            ):
                raise PreboardError("recovery result does not match the plan")
            commit = git_identity(repository)
        except PreboardError:
            raise
        except Exception as error:
            raise PreboardError(f"preboard validation failed: {error}") from error

        permit = PreboardPermit(
            1,
            True,
            "preboard-pass",
            plan.plan_sha256,
            desktop_hash,
            recovery_hash,
            commit,
            identities["kernel"].sha256,
            tuple((name, identities[name].crc32) for name in _TRANSFER_NAMES),
            plan.bootargs,
            plan.reboot_after,
        )
        permit.validate()
        publication.write(permit.canonical_bytes())
        return permit
