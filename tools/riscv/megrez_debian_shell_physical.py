#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed physical two-boot evidence for the Megrez Debian shell."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import time
from typing import Any, Protocol

from tools.riscv.debian.rootfs.gate_protocol import (
    MAX_TRANSCRIPT_BYTES,
    GateResult,
    classify_boot,
    shell_commands,
)
from tools.riscv.debian.rootfs.gate_runtime import SerialConsole
from tools.riscv.megrez_debian_shell_contract import (
    P2_NR_SECTORS,
    P2_START_LBA,
    PersistentShellPlan,
)
from tools.riscv.megrez_debian_shell_evidence import ShellPermit


_READY_MARKER = b"__DEBIAN_ROOTFS_SHELL_READY__"
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_REASON_RE = re.compile(r"\A[a-z][a-z0-9-]*\Z")
_INTERFACE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}\Z")
_RESULT_KEYS = {
    "schema_version",
    "passed",
    "reason",
    "plan_sha256",
    "permit_sha256",
    "inventory_sha256",
    "nonce_sha256",
    "boot1_serial_sha256",
    "boot2_serial_sha256",
    "boot1_recovered",
    "boot2_recovered",
}
_FATAL_LOG_MARKERS = (
    b"kernel panic",
    b"oops:",
    b"ext2-fs error",
    b"ext2-fs warning",
    b"buffer i/o error",
    b"blk_update_request: i/o error",
    b"debian_rootfs_fail reason=",
)


class PhysicalShellError(ValueError):
    """Physical evidence differs from the frozen safe workflow."""


class ShellSession(Protocol):
    fd: int

    def _log(self, text: str) -> None: ...


@dataclass(frozen=True)
class PhysicalBoot:
    """One fully drained shell attempt plus its recovery observation."""

    protocol_transcript: bytes
    complete_transcript: bytes
    recovered: bool

    def validate(self) -> None:
        for role, transcript in (
            ("protocol", self.protocol_transcript),
            ("complete", self.complete_transcript),
        ):
            if not isinstance(transcript, bytes):
                raise PhysicalShellError(f"{role} transcript must be bytes")
            if len(transcript) > MAX_TRANSCRIPT_BYTES:
                raise PhysicalShellError(f"{role} transcript exceeds 8 MiB")
        if not isinstance(self.recovered, bool):
            raise PhysicalShellError("recovery evidence must be a boolean")


@dataclass(frozen=True)
class PhysicalShellResult:
    """Canonical result published after both physical boot epochs."""

    schema_version: int
    passed: bool
    reason: str
    plan_sha256: str
    permit_sha256: str
    inventory_sha256: str
    nonce_sha256: str
    boot1_serial_sha256: str
    boot2_serial_sha256: str
    boot1_recovered: bool
    boot2_recovered: bool

    def validate(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise PhysicalShellError("schema_version must be 1")
        if not isinstance(self.passed, bool):
            raise PhysicalShellError("passed must be a boolean")
        if (
            not isinstance(self.reason, str)
            or _REASON_RE.fullmatch(self.reason) is None
        ):
            raise PhysicalShellError("reason must be a stable lowercase token")
        for field in (
            "plan_sha256",
            "permit_sha256",
            "inventory_sha256",
            "nonce_sha256",
            "boot1_serial_sha256",
            "boot2_serial_sha256",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise PhysicalShellError(f"{field} must be lowercase hexadecimal")
        if not isinstance(self.boot1_recovered, bool) or not isinstance(
            self.boot2_recovered, bool
        ):
            raise PhysicalShellError("recovery fields must be booleans")
        if self.passed:
            if self.reason != "pass" or not (
                self.boot1_recovered and self.boot2_recovered
            ):
                raise PhysicalShellError("passing result requires both recovery epochs")
        elif self.reason == "pass":
            raise PhysicalShellError("failed result cannot use the pass reason")

    def canonical_bytes(self) -> bytes:
        self.validate()
        return (
            json.dumps(
                {
                    "schema_version": self.schema_version,
                    "passed": self.passed,
                    "reason": self.reason,
                    "plan_sha256": self.plan_sha256,
                    "permit_sha256": self.permit_sha256,
                    "inventory_sha256": self.inventory_sha256,
                    "nonce_sha256": self.nonce_sha256,
                    "boot1_serial_sha256": self.boot1_serial_sha256,
                    "boot2_serial_sha256": self.boot2_serial_sha256,
                    "boot1_recovered": self.boot1_recovered,
                    "boot2_recovered": self.boot2_recovered,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> PhysicalShellResult:
        try:
            document = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PhysicalShellError("invalid physical result JSON") from error
        if not isinstance(document, dict) or set(document) != _RESULT_KEYS:
            raise PhysicalShellError("physical result fields differ")
        result = cls(**document)
        result.validate()
        if result.canonical_bytes() != payload:
            raise PhysicalShellError("physical result must use canonical JSON")
        return result


class PhysicalOperations(Protocol):
    """Side effects required by the pure two-boot state machine."""

    def invalidate(self) -> None: ...

    def validate_artifacts(
        self, plan: PersistentShellPlan
    ) -> tuple[str, tuple[tuple[str, str], ...]]: ...

    def run_boot(
        self, plan: PersistentShellPlan, boot_number: int, nonce: str
    ) -> PhysicalBoot: ...

    def publish(
        self, logs: tuple[bytes, bytes], result: PhysicalShellResult
    ) -> None: ...


def run_debian_shell_phase(
    session: ShellSession,
    *,
    boot_number: int,
    nonce: str,
    debian_release: str,
    packages: Iterable[tuple[str, str]],
    deadline: float,
    reboot: bool,
) -> GateResult:
    """Drive one already-booted Debian serial shell with a total deadline."""

    if not isinstance(reboot, bool):
        raise ValueError("reboot must be a boolean")
    commands = shell_commands(boot_number=boot_number, nonce=nonce)
    console = SerialConsole(session.fd, max_bytes=MAX_TRANSCRIPT_BYTES)
    console.wait_for(_READY_MARKER, deadline)
    for command in commands:
        start = console.checkpoint()
        console.send(command.payload.encode() + b"\n", deadline)
        console.wait_for(command.end_marker.encode(), deadline, start=start)
    console.drain(min(deadline, time.monotonic() + 0.05))
    transcript = console.transcript
    session._log(transcript.decode("utf-8", errors="replace"))
    result = classify_boot(
        transcript,
        commands,
        boot_number=boot_number,
        expected_debian_release=debian_release,
        expected_packages=packages,
        expected_nonce=nonce,
    )
    if result.passed and reboot:
        console.send(b"sync; reboot -f\n", deadline)
    return result


def dnsmasq_tftp_argv(interface: str, root: Path) -> tuple[str, ...]:
    """Return a TFTP-only dnsmasq command that cannot configure DHCP or DNS."""

    if not isinstance(interface, str) or _INTERFACE_RE.fullmatch(interface) is None:
        raise PhysicalShellError("unsafe TFTP interface name")
    root = Path(root)
    if not root.is_absolute() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in str(root)
    ):
        raise PhysicalShellError("TFTP root must be an absolute safe path")
    return (
        "/usr/sbin/dnsmasq",
        "--no-daemon",
        "--port=0",
        "--no-hosts",
        "--no-resolv",
        f"--interface={interface}",
        "--bind-interfaces",
        "--enable-tftp",
        f"--tftp-root={root}",
        "--log-facility=-",
    )


def run_physical_gate(
    plan: PersistentShellPlan,
    permit: ShellPermit,
    inventory: object,
    operations: PhysicalOperations,
    *,
    nonce_factory: Callable[[], str] = lambda: secrets.token_hex(32),
) -> PhysicalShellResult:
    """Run two physical boots, requiring a fresh recovery epoch after each."""

    operations.invalidate()
    nonce = nonce_factory()
    nonce_sha256 = _digest(nonce.encode())
    logs = [b"", b""]
    recovered = [False, False]
    permit_sha256 = _digest(permit.canonical_bytes())
    inventory_payload = inventory.canonical_bytes()
    inventory_sha256 = _digest(inventory_payload)
    reason = "preflight-failed"
    passed = False
    try:
        _validate_preflight(plan, permit, inventory, permit_sha256)
        release, packages = operations.validate_artifacts(plan)
        for boot_number in (1, 2):
            reason = f"boot{boot_number}-failed"
            boot = operations.run_boot(plan, boot_number, nonce)
            if not isinstance(boot, PhysicalBoot):
                raise PhysicalShellError("physical boot returned an invalid type")
            boot.validate()
            logs[boot_number - 1] = boot.complete_transcript
            recovered[boot_number - 1] = boot.recovered
            commands = shell_commands(boot_number=boot_number, nonce=nonce)
            classification = classify_boot(
                boot.protocol_transcript,
                commands,
                boot_number=boot_number,
                expected_debian_release=release,
                expected_packages=packages,
                expected_nonce=nonce,
            )
            if not classification.passed:
                reason = f"boot{boot_number}-protocol-failed"
                raise PhysicalShellError(classification.reason)
            if _contains_fatal(boot.complete_transcript):
                reason = f"boot{boot_number}-fatal-transcript"
                raise PhysicalShellError("physical transcript contains a fatal marker")
            if not boot.recovered:
                reason = f"boot{boot_number}-recovery-failed"
                raise PhysicalShellError("fresh recovery epoch was not observed")
        reason = "pass"
        passed = True
    except (OSError, RuntimeError, PhysicalShellError, ValueError, TypeError):
        passed = False

    redacted = tuple(log.replace(nonce.encode(), b"<redacted-nonce>") for log in logs)
    result = PhysicalShellResult(
        schema_version=1,
        passed=passed,
        reason=reason,
        plan_sha256=plan.plan_sha256,
        permit_sha256=permit_sha256,
        inventory_sha256=inventory_sha256,
        nonce_sha256=nonce_sha256,
        boot1_serial_sha256=_digest(redacted[0]),
        boot2_serial_sha256=_digest(redacted[1]),
        boot1_recovered=recovered[0],
        boot2_recovered=recovered[1],
    )
    result.validate()
    operations.publish(redacted, result)
    return result


def _validate_preflight(
    plan: PersistentShellPlan,
    permit: ShellPermit,
    inventory: object,
    permit_sha256: str,
) -> None:
    plan.validate()
    permit.validate()
    artifacts = plan.artifact_map()
    if (
        permit.plan_sha256 != plan.plan_sha256
        or permit.git_commit != plan.git_commit
        or permit.megrez_kernel_sha256 != artifacts["megrez_kernel"].sha256
        or permit.stage1_crc32 != artifacts["stage1"].crc32
        or permit.megrez_dtb_crc32 != artifacts["megrez_dtb"].crc32
        or permit.root_image_sha256 != artifacts["root_image"].sha256
        or permit.gate_bootargs != plan.gate_bootargs
        or permit.gate_reboot_after != plan.gate_reboot_after
        or permit.long_operation_reboot_after != plan.long_operation_reboot_after
    ):
        raise PhysicalShellError("shell permit differs from the frozen plan")
    inventory.validate()
    if (
        inventory.status != "matching"
        or inventory.plan_sha256 != plan.plan_sha256
        or inventory.permit_sha256 != permit_sha256
        or inventory.expected_root_sha256 != artifacts["root_image"].sha256
    ):
        raise PhysicalShellError("inventory does not authorize a physical boot")
    partitions = inventory.partitions
    if (
        not isinstance(partitions, tuple)
        or len(partitions) != 3
        or tuple(partition.number for partition in partitions) != (1, 2, 3)
        or (partitions[1].start_lba, partitions[1].nr_sectors)
        != (P2_START_LBA, P2_NR_SECTORS)
    ):
        raise PhysicalShellError("inventory partition geometry differs")


def _contains_fatal(transcript: bytes) -> bool:
    lowered = transcript.lower()
    return any(marker in lowered for marker in _FATAL_LOG_MARKERS)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PhysicalShellError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
