#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed host protocol for the Megrez RockOS control-chain check.

This module deliberately contains no board I/O.  It defines the bounded shell
frame, the ordered reboot/reconnect state machine, watchdog budget checks, and
the output-directory ownership used by a physical runner.  A runner can use
these pieces with either serial or SSH adapters without making an ambiguous
prompt look like a successful hand-off.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory


class ControlChainError(ValueError):
    """One control-chain contract or state transition is invalid."""


class ControlStage(str, Enum):
    SERIAL_BEFORE_REBOOT = "serial-before-reboot"
    SERIAL_AFTER_REBOOT = "serial-after-reboot"
    SERIAL_REOPEN = "serial-reopen"
    SSH_AFTER_REBOOT = "ssh-after-reboot"


_NONCE = re.compile(r"\A[0-9a-f]{32}\Z")
_BOOT_ID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_FRAME = re.compile(
    r"\AASTERINAS_CONTROL v=1 "
    r"stage=(serial-before-reboot|serial-after-reboot|serial-reopen|ssh-after-reboot) "
    r"nonce=([0-9a-f]{32}) uid=([0-9]+) "
    r"boot_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) "
    r"status=(pass|fail)\n\Z"
)
@dataclass(frozen=True)
class ControlReceipt:
    """One authenticated, nonce-bound control response."""

    stage: ControlStage
    nonce: str
    uid: int
    boot_id: str


def _validate_nonce(nonce: str) -> None:
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ControlChainError("control nonce must be 32 lowercase hex digits")


def build_handshake_command(stage: ControlStage, nonce: str) -> str:
    """Build a bounded POSIX-shell command that emits one control frame."""

    if not isinstance(stage, ControlStage):
        raise ControlChainError("control stage is invalid")
    _validate_nonce(nonce)
    command = (
        "_asterinas_uid=$(id -u); "
        "_asterinas_boot=$(cat /proc/sys/kernel/random/boot_id); "
        "_asterinas_status=fail; "
        '[ "$_asterinas_uid" = 0 ] && [ -n "$_asterinas_boot" ] && '
        "_asterinas_status=pass; "
        "printf 'ASTERINAS_CONTROL v=1 "
        f"stage={stage.value} nonce={nonce} uid=%s boot_id=%s status=%s\\n' "
        '"$_asterinas_uid" "$_asterinas_boot" "$_asterinas_status"'
    )
    if len((command + "\n").encode()) > 1024:
        raise ControlChainError("control command exceeds serial line budget")
    return command


def parse_handshake_frame(payload: str | bytes) -> ControlReceipt:
    """Parse exactly one frame and fail closed on extra or malformed output."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise ControlChainError("control frame is not ASCII") from error
    elif isinstance(payload, str):
        text = payload
    else:
        raise ControlChainError("control frame must be text or bytes")
    match = _FRAME.fullmatch(text)
    if match is None:
        raise ControlChainError("control frame is malformed or contains extra fields")
    stage = ControlStage(match.group(1))
    nonce = match.group(2)
    uid = int(match.group(3), 10)
    boot_id = match.group(4)
    status = match.group(5)
    if uid != 0 or status != "pass":
        raise ControlChainError("control frame does not prove UID 0")
    if not _BOOT_ID.fullmatch(boot_id):
        raise ControlChainError("control frame boot-id is invalid")
    return ControlReceipt(stage=stage, nonce=nonce, uid=uid, boot_id=boot_id)


def parse_transcript_frame(
    payload: str | bytes, *, stage: ControlStage, nonce: str
) -> ControlReceipt:
    """Extract exactly one expected frame from a bounded serial transcript.

    Boot logs and shell echo are allowed around the frame.  Any second control
    frame, malformed control-looking line, stale nonce, or wrong stage fails
    closed instead of letting an old response satisfy a new command.
    """

    if not isinstance(stage, ControlStage):
        raise ControlChainError("control stage is invalid")
    _validate_nonce(nonce)
    if isinstance(payload, bytes):
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise ControlChainError("control transcript is not ASCII") from error
    elif isinstance(payload, str):
        text = payload
    else:
        raise ControlChainError("control transcript must be text or bytes")

    records: list[ControlReceipt] = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("ASTERINAS_CONTROL "):
            continue
        normalized = line[:-2] + "\n" if line.endswith("\r\n") else line
        records.append(parse_handshake_frame(normalized))
    if len(records) != 1:
        raise ControlChainError("control transcript must contain exactly one frame")
    receipt = records[0]
    if receipt.stage is not stage or receipt.nonce != nonce:
        raise ControlChainError("control transcript frame is stale or out of phase")
    return receipt


def parse_recovery_marker(
    payload: str, *, prefix: str, nonce: str
) -> ControlReceipt:
    """Validate one legacy runner recovery response, never a login prompt.

    This proves root command access on one connection only. A runner still
    needs the ordered ControlChain to prove reboot and serial-reopen access.
    """

    _validate_nonce(nonce)
    if prefix not in (
        "ASTERINAS_DISPLAY_RECOVERY", "ASTERINAS_SINGLE_PACKET_RECOVERY"
    ):
        raise ControlChainError("unknown recovery marker")
    lines = [line for line in payload.splitlines() if line.startswith(prefix + " ")]
    if len(lines) != 1:
        raise ControlChainError("recovery transcript must contain exactly one marker")
    match = re.fullmatch(
        re.escape(prefix) + r" nonce=([0-9a-f]{32}) uid=([0-9]+) boot_id=(\S+)",
        lines[0],
    )
    if match is None or match[1] != nonce:
        raise ControlChainError("recovery marker is malformed or stale")
    if match[2] != "0":
        raise ControlChainError("recovery marker does not prove UID 0")
    if _BOOT_ID.fullmatch(match[3]) is None:
        raise ControlChainError("recovery marker boot-id is invalid")
    return ControlReceipt(ControlStage.SERIAL_AFTER_REBOOT, nonce, 0, match[3])


class ControlChain:
    """Ordered state machine for one RockOS reboot and reconnect sequence."""

    _ORDER = tuple(ControlStage)

    def __init__(self) -> None:
        self._records: list[ControlReceipt] = []
        self._reboot_sent = False
        self._old_boot_id: str | None = None
        self._new_boot_id: str | None = None

    @property
    def records(self) -> tuple[ControlReceipt, ...]:
        return tuple(self._records)

    def mark_reboot_sent(self) -> None:
        if self._reboot_sent or len(self._records) != 1:
            raise ControlChainError("reboot must follow exactly one pre-reboot frame")
        if self._records[0].stage is not ControlStage.SERIAL_BEFORE_REBOOT:
            raise ControlChainError("pre-reboot serial frame is missing")
        self._reboot_sent = True

    def record(self, receipt: ControlReceipt) -> None:
        if not isinstance(receipt, ControlReceipt):
            raise ControlChainError("control receipt type is invalid")
        if receipt.uid != 0 or not _BOOT_ID.fullmatch(receipt.boot_id):
            raise ControlChainError("control receipt does not prove UID 0 and boot-id")
        _validate_nonce(receipt.nonce)
        index = len(self._records)
        if index >= len(self._ORDER):
            raise ControlChainError("control chain has too many frames")
        if receipt.stage is not self._ORDER[index]:
            raise ControlChainError(
                f"expected {self._ORDER[index].value}, got {receipt.stage.value}"
            )
        if any(existing.nonce == receipt.nonce for existing in self._records):
            raise ControlChainError("control nonce was reused")
        if index == 0:
            self._old_boot_id = receipt.boot_id
        elif index == 1:
            if not self._reboot_sent:
                raise ControlChainError("post-reboot frame arrived before reboot")
            if receipt.boot_id == self._old_boot_id:
                raise ControlChainError("reboot did not produce a new boot-id")
            self._new_boot_id = receipt.boot_id
        else:
            if receipt.boot_id != self._new_boot_id:
                raise ControlChainError("reconnect frame changed boot-id")
        self._records.append(receipt)

    def finish(self) -> dict[str, Any]:
        if not self._reboot_sent or len(self._records) != len(self._ORDER):
            raise ControlChainError("control chain is incomplete")
        assert self._old_boot_id is not None and self._new_boot_id is not None
        return {
            "schema_version": 1,
            "status": "pass",
            "old_boot_id": self._old_boot_id,
            "new_boot_id": self._new_boot_id,
            "stages": [receipt.stage.value for receipt in self._records],
            "nonce_sha256": [
                hashlib.sha256(receipt.nonce.encode("ascii")).hexdigest()
                for receipt in self._records
            ],
        }


@dataclass(frozen=True)
class ControlChainBudget:
    """Deadlines shared by the runner and its watchdog/recovery contract."""

    boot_timeout: float = 120.0
    drain_timeout: float = 2.0
    headroom: float = 30.0

    def __post_init__(self) -> None:
        values = (self.boot_timeout, self.drain_timeout, self.headroom)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ControlChainError("control deadlines must be finite and positive")

    @property
    def minimum_reboot_after(self) -> float:
        return self.boot_timeout + self.drain_timeout + self.headroom

    def validate_reboot_after(self, reboot_after: float) -> None:
        if (
            isinstance(reboot_after, bool)
            or not isinstance(reboot_after, (int, float))
            or not math.isfinite(reboot_after)
            or reboot_after < self.minimum_reboot_after
        ):
            raise ControlChainError(
                "reboot-after does not leave the required recovery headroom"
            )


def _safe_output_path(path: Path, repository: Path) -> Path:
    if ".." in path.parts or ".." in repository.parts:
        raise ControlChainError("control output path must not contain parent traversal")
    repository = repository.absolute()
    candidate = path.absolute()
    allowed = repository / "target"
    try:
        relative = candidate.relative_to(allowed)
    except ValueError as error:
        raise ControlChainError("control output must be under repository target") from error
    if not relative.parts:
        raise ControlChainError("control output must be a run directory below target")
    current = repository
    for component in ("target", *relative.parts):
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ControlChainError("control output path contains an unsafe component")
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    candidate.chmod(0o700)
    return candidate


class ControlChainOutput:
    """Exclusive, bounded output owner for one control-chain run."""

    def __init__(self, path: Path, *, repository: Path) -> None:
        safe_path = _safe_output_path(path, repository)
        self.path = safe_path
        self._output = PinnedOutputDirectory(safe_path)
        self._prepared = False

    def __enter__(self) -> "ControlChainOutput":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def preflight(self) -> None:
        if self._prepared:
            raise ControlChainError("control output was already preflighted")
        try:
            self._output.lock_exclusive()
            if os.listdir(self._output._operation_fd):
                raise ControlChainError("control output already contains evidence")
        except BaseException:
            self._output.close()
            raise
        self._prepared = True

    def write(self, name: str, payload: bytes) -> None:
        if not self._prepared:
            raise ControlChainError("control output preflight is required")
        self._output.atomic_write(name, payload)

    def close(self) -> None:
        self._output.close()
