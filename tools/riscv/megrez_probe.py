#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded kernel probes for the current Megrez MMC deployment."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import select
import stat
import sys
import time
from typing import Any, Callable, Protocol, Sequence
import tty
import zlib

from tools.riscv.debian.rootfs.gate_protocol import GENERIC_SV39_CPU
from tools.riscv.debian.rootfs.gate_runtime import (
    GateProcess,
    PinnedOutputDirectory,
    SerialConsole,
    launch_process,
)
from tools.riscv.megrez_board_session import (
    BoardSession,
    open_serial,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debug_board import _lock_serial, _uboot_bootargs_commands
from tools.riscv.megrez_debug_contract import DebugContractError, DebugPlan


_BUNDLE_FIELDS = frozenset(
    ("schema_version", "plan", "plan_sha256", "device", "mmc_artifacts")
)
_MMC_FIELDS = frozenset(("name", "path"))
_MMC_ORDER = ("kernel", "initramfs", "megrez_dtb")
_MMC_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*")
_SERIAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_SAFE_DETAIL = r"[a-z0-9][a-z0-9-]*"
_START = re.compile(
    rb"ASTERINAS_PROBE_START v=1 nonce=([0-9a-f]{32}) seq=([0-9]+) "
    rb"name=([a-z0-9-]+)"
)
_RUN = re.compile(
    rb"ASTERINAS_PROBE_RUN v=1 nonce=([0-9a-f]{32}) "
    rb"probes=([a-z0-9,-]+) shell=([01])"
)
_PASS = re.compile(
    rb"ASTERINAS_PROBE_PASS v=1 nonce=([0-9a-f]{32}) seq=([0-9]+) "
    rb"name=([a-z0-9-]+) detail=(" + _SAFE_DETAIL.encode() + rb")"
)
_FAIL = re.compile(
    rb"ASTERINAS_PROBE_FAIL v=1 nonce=([0-9a-f]{32}) seq=([0-9]+) "
    rb"name=([a-z0-9-]+) errno=([0-9]+) detail=(" + _SAFE_DETAIL.encode() + rb")"
)
_DMESG_BEGIN = re.compile(
    rb"ASTERINAS_PROBE_DMESG_BEGIN v=1 nonce=([0-9a-f]{32}) bytes=([0-9]+)"
)
_DMESG_END = re.compile(rb"ASTERINAS_PROBE_DMESG_END v=1 nonce=([0-9a-f]{32})")
_DONE = re.compile(
    rb"ASTERINAS_PROBE_DONE v=1 nonce=([0-9a-f]{32}) count=([0-9]+) "
    rb"status=(pass|fail)"
)
_REBOOT_READY = re.compile(rb"ASTERINAS_PROBE_REBOOT_READY v=1 nonce=([0-9a-f]{32})")
_SHELL_READY = re.compile(rb"ASTERINAS_PROBE_SHELL_READY v=1 nonce=([0-9a-f]{32})")
_SHELL_COMMANDS_RECORD = (
    b"ASTERINAS_PROBE_SHELL_COMMANDS "
    b"help,dmesg,mounts,boot,syscall213,syscall272,"
    b"ext2-writeback,systemd-compat,exit"
)
_SHELL_RESULT = re.compile(
    rb"ASTERINAS_PROBE_SHELL_RESULT name=([a-z0-9-]+) "
    rb"status=(pass|fail) errno=([0-9]+) detail=(" + _SAFE_DETAIL.encode() + rb")"
)
_SHELL_REJECT = re.compile(
    rb"ASTERINAS_PROBE_SHELL_REJECT reason=(" + _SAFE_DETAIL.encode() + rb")"
)
_SHELL_MOUNTS_LINE = re.compile(
    rb"ASTERINAS_PROBE_SHELL_MOUNTS [^\r\n ]+ [^\r\n ]+ [^\r\n ]+"
)
_SHELL_MOUNTS_BEGIN = b"ASTERINAS_PROBE_SHELL_MOUNTS_BEGIN"
_SHELL_MOUNTS_END = b"ASTERINAS_PROBE_SHELL_MOUNTS_END"
_PROTOCOL_NONCE = re.compile(rb"ASTERINAS_PROBE_[^\r\n]*nonce=([0-9a-f]{32})")
_PROTOCOL_PREFIX = b"ASTERINAS_PROBE_"
_READY = b"ASTERINAS_PROBE_READY v=1 pid=1"
_MAX_TRANSCRIPT_BYTES = 256 * 1024
_MAX_DMESG_BYTES = 32 * 1024
_SERIAL_CONTEXT_BYTES = 2048
_SERIAL_SUMMARY_BYTES = 8 * 1024
_PHYSICAL_TRANSCRIPT_BYTES = 256 * 1024
_PROBE_READY = b"ASTERINAS_PROBE_READY v=1 pid=1"
_SOFTWARE_REBOOT_ARMED_30 = b"ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=30"
_FATAL_REBOOT_MARKERS = (
    b"kernel panic",
    b"uncaught panic:",
    b"not syncing",
    b"oops:",
    b"fatal exception",
)
_MIN_DEADLINE_REBOOT_SECONDS = 29.0
_SHELL_READY_PREFIX = b"ASTERINAS_PROBE_SHELL_READY v=1 nonce="
_SHELL_COMMANDS = frozenset(
    (
        "help",
        "dmesg",
        "mounts",
        "boot",
        "syscall213",
        "syscall272",
        "ext2-writeback",
        "systemd-compat",
        "exit",
    )
)


class ProbeContractError(ValueError):
    """A fast-probe input does not satisfy the immutable contract."""


class ProbeProtocolError(ValueError):
    """Serial evidence does not prove one complete requested exchange."""


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


@dataclass(frozen=True)
class ProbeOutcome:
    """One terminal result bound to a requested sequence entry."""

    sequence: int
    name: str
    passed: bool
    error_number: int | None
    detail: str


@dataclass(frozen=True)
class ProbeExchange:
    """The complete fail-closed classification of one guest batch."""

    outcomes: tuple[ProbeOutcome, ...]
    passed: bool
    dmesg: bytes


@dataclass(frozen=True)
class ProbeRunConfig:
    """All bounded host and guest deadlines for one probe boot."""

    session_seconds: int = 90
    recovery_seconds: int = 30
    open_seconds: int = 60
    artifact_seconds: int = 60
    shell: bool = False

    def __post_init__(self) -> None:
        validate_session_seconds(self.session_seconds)
        if (
            type(self.recovery_seconds) is not int
            or not 1 <= self.recovery_seconds <= 120
            or type(self.open_seconds) is not int
            or not 1 <= self.open_seconds <= 120
            or type(self.artifact_seconds) is not int
            or not 1 <= self.artifact_seconds <= 300
        ):
            raise ProbeContractError("host deadlines are outside the bounded range")
        if type(self.shell) is not bool:
            raise ProbeContractError("shell selector must be boolean")


@dataclass(frozen=True)
class ProbeRunResult:
    """One terminal result published only after recovery is classified."""

    schema_version: int
    passed: bool
    reason: str
    bundle_sha256: str
    plan_sha256: str
    selected_probes: tuple[str, ...]
    outcomes: tuple[ProbeOutcome, ...]
    elapsed_seconds: float
    recovered: bool

    def to_dict(self) -> dict[str, object]:
        if self.schema_version != 1:
            raise ProbeContractError("probe result schema must be 1")
        return {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "reason": self.reason,
            "bundle_sha256": self.bundle_sha256,
            "plan_sha256": self.plan_sha256,
            "selected_probes": list(self.selected_probes),
            "outcomes": [
                {
                    "sequence": outcome.sequence,
                    "name": outcome.name,
                    "passed": outcome.passed,
                    "errno": outcome.error_number,
                    "detail": outcome.detail,
                }
                for outcome in self.outcomes
            ],
            "elapsed_seconds": self.elapsed_seconds,
            "recovered": self.recovered,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())


class ProbeOperations(Protocol):
    @property
    def guest_started(self) -> bool: ...

    @property
    def transcript(self) -> bytes: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, timeout: float) -> tuple[str, ...]: ...

    def boot(self, bootargs: str, timeout: float) -> None: ...

    def exchange(
        self,
        nonce: str,
        selected: tuple[ProbeDefinition, ...],
        shell: bool,
        timeout: float,
    ) -> ProbeExchange: ...

    def request_reboot(self, nonce: str, timeout: float) -> None: ...

    def await_recovery(self, timeout: float) -> None: ...

    def close(self) -> None: ...


class ProbePublisher(Protocol):
    def invalidate(self) -> None: ...

    def publish(
        self, result: ProbeRunResult, transcript: bytes, dmesg: bytes
    ) -> None: ...


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


def _validate_nonce(nonce: object) -> str:
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ProbeContractError("probe nonce must be 32 lowercase hexadecimal digits")
    return nonce


def encode_probe_request(
    nonce: str,
    selected: Sequence[ProbeDefinition],
    *,
    shell: bool = False,
) -> bytes:
    """Encode one fixed registry selection without executable guest text."""

    nonce = _validate_nonce(nonce)
    if type(shell) is not bool:
        raise ProbeContractError("shell selector must be boolean")
    names = tuple(item.name for item in selected)
    canonical = validate_probe_names(names)
    if tuple(selected) != canonical:
        raise ProbeContractError("probe definitions do not match the fixed registry")
    payload = (
        f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes={','.join(names)} "
        f"shell={int(shell)}\n"
    ).encode("ascii")
    if len(payload) > 512:
        raise ProbeContractError("probe request exceeds 512 bytes")
    return payload


def _selected_names(selected: Sequence[str]) -> tuple[str, ...]:
    definitions = validate_probe_names(selected)
    return tuple(item.name for item in definitions)


def _next_protocol_line(lines: list[bytes], cursor: int) -> tuple[int, bytes]:
    while cursor < len(lines):
        line = lines[cursor].removesuffix(b"\n")
        if line.startswith(_PROTOCOL_PREFIX):
            return cursor, line
        cursor += 1
    raise ProbeProtocolError("probe transcript has no terminal protocol record")


def _require_nonce(match: re.Match[bytes], nonce: bytes) -> None:
    if match.group(1) != nonce:
        raise ProbeProtocolError("probe protocol contains a stale nonce")


def classify_probe_transcript(
    transcript: bytes,
    nonce: str,
    selected: Sequence[str],
    *,
    shell: bool = False,
) -> ProbeExchange:
    """Validate exactly one ordered exchange from noisy serial output."""

    if not isinstance(transcript, bytes) or len(transcript) > _MAX_TRANSCRIPT_BYTES:
        raise ProbeProtocolError("probe transcript must be bounded bytes")
    try:
        transcript.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProbeProtocolError("probe transcript is not UTF-8") from error
    nonce_text = _validate_nonce(nonce)
    nonce_bytes = nonce_text.encode()
    names = _selected_names(selected)
    normalized = transcript.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = normalized.splitlines(keepends=True)
    if sum(line.removesuffix(b"\n") == _READY for line in lines) != 1:
        raise ProbeProtocolError("probe readiness marker is missing or duplicated")
    observed_nonces = set(_PROTOCOL_NONCE.findall(normalized))
    if observed_nonces - {nonce_bytes}:
        raise ProbeProtocolError("probe protocol contains a stale nonce")
    unknown = []
    known_patterns = (
        _RUN,
        _START,
        _PASS,
        _FAIL,
        _DMESG_BEGIN,
        _DMESG_END,
        _DONE,
        _REBOOT_READY,
        _SHELL_READY,
        _SHELL_RESULT,
        _SHELL_REJECT,
        _SHELL_MOUNTS_LINE,
    )
    for line in lines:
        record = line.removesuffix(b"\n")
        if (
            record.startswith(_PROTOCOL_PREFIX)
            and record != _READY
            and record
            not in (_SHELL_COMMANDS_RECORD, _SHELL_MOUNTS_BEGIN, _SHELL_MOUNTS_END)
            and not any(pattern.fullmatch(record) for pattern in known_patterns)
        ):
            unknown.append(record)
    if unknown:
        raise ProbeProtocolError("probe transcript contains an unknown protocol marker")

    ready_index = next(
        index for index, line in enumerate(lines) if line.removesuffix(b"\n") == _READY
    )
    if any(
        line.removesuffix(b"\n").startswith(_PROTOCOL_PREFIX)
        for line in lines[:ready_index]
    ):
        raise ProbeProtocolError("probe protocol preceded the readiness marker")
    cursor = ready_index + 1
    run_indexes = [
        index
        for index, line in enumerate(lines)
        if _RUN.fullmatch(line.removesuffix(b"\n")) is not None
    ]
    if len(run_indexes) > 1:
        raise ProbeProtocolError("probe request echo was replayed")
    if run_indexes:
        run_index, run_record = _next_protocol_line(lines, cursor)
        if run_index != run_indexes[0]:
            raise ProbeProtocolError("probe request echo is out of order")
        request_echo = _RUN.fullmatch(run_record)
        assert request_echo is not None
        _require_nonce(request_echo, nonce_bytes)
        if request_echo.group(2).decode() != ",".join(names):
            raise ProbeProtocolError("probe request echo changed the selection")
        if request_echo.group(3) != (b"1" if shell else b"0"):
            raise ProbeProtocolError("probe request echo changed the shell selector")
        cursor = run_index + 1
    outcomes: list[ProbeOutcome] = []
    failed = False
    while len(outcomes) < len(names):
        start_index, record = _next_protocol_line(lines, cursor)
        start = _START.fullmatch(record)
        if start is None:
            break
        _require_nonce(start, nonce_bytes)
        sequence = int(start.group(2))
        name = start.group(3).decode()
        if sequence != len(outcomes) or name != names[sequence] or failed:
            raise ProbeProtocolError("probe start record is reordered or substituted")
        terminal_index, terminal_record = _next_protocol_line(lines, start_index + 1)
        passed = _PASS.fullmatch(terminal_record)
        failure = _FAIL.fullmatch(terminal_record)
        if passed is None and failure is None:
            raise ProbeProtocolError("probe terminal record is missing or reordered")
        terminal = passed if passed is not None else failure
        assert terminal is not None
        _require_nonce(terminal, nonce_bytes)
        if int(terminal.group(2)) != sequence or terminal.group(3).decode() != name:
            raise ProbeProtocolError("probe terminal identity was substituted")
        if passed is not None:
            outcomes.append(
                ProbeOutcome(sequence, name, True, None, terminal.group(4).decode())
            )
        else:
            failed = True
            outcomes.append(
                ProbeOutcome(
                    sequence,
                    name,
                    False,
                    int(terminal.group(4)),
                    terminal.group(5).decode(),
                )
            )
        cursor = terminal_index + 1
        if failed:
            break

    dmesg = b""
    if failed:
        begin_index, begin_record = _next_protocol_line(lines, cursor)
        begin = _DMESG_BEGIN.fullmatch(begin_record)
        if begin is None:
            raise ProbeProtocolError("failed probe has no bounded dmesg frame")
        _require_nonce(begin, nonce_bytes)
        announced = int(begin.group(2))
        if announced > _MAX_DMESG_BYTES:
            raise ProbeProtocolError("probe dmesg frame exceeds the byte cap")
        end_index = begin_index + 1
        while end_index < len(lines):
            candidate = lines[end_index].removesuffix(b"\n")
            if _DMESG_END.fullmatch(candidate) is not None:
                break
            end_index += 1
        if end_index == len(lines):
            raise ProbeProtocolError("probe dmesg end marker is missing")
        end = _DMESG_END.fullmatch(lines[end_index].removesuffix(b"\n"))
        assert end is not None
        _require_nonce(end, nonce_bytes)
        dmesg = b"".join(lines[begin_index + 1 : end_index])
        if len(dmesg) != announced:
            raise ProbeProtocolError("probe dmesg byte count does not match")
        cursor = end_index + 1

    done_index, done_record = _next_protocol_line(lines, cursor)
    done = _DONE.fullmatch(done_record)
    if done is None:
        raise ProbeProtocolError("probe DONE record is missing or reordered")
    _require_nonce(done, nonce_bytes)
    status = done.group(3) == b"pass"
    if int(done.group(2)) != len(outcomes) or status == failed:
        raise ProbeProtocolError("probe DONE count or status is inconsistent")
    if status and len(outcomes) != len(names):
        raise ProbeProtocolError("successful exchange omitted a requested probe")
    cursor = done_index + 1
    if shell:
        shell_index, shell_record = _next_protocol_line(lines, cursor)
        shell_ready = _SHELL_READY.fullmatch(shell_record)
        if shell_ready is None:
            raise ProbeProtocolError("probe shell readiness is missing")
        _require_nonce(shell_ready, nonce_bytes)
        cursor = shell_index + 1
        while True:
            record_index, record = _next_protocol_line(lines, cursor)
            if _REBOOT_READY.fullmatch(record) is not None:
                break
            if record == _SHELL_COMMANDS_RECORD:
                cursor = record_index + 1
                continue
            if _SHELL_RESULT.fullmatch(record) or _SHELL_REJECT.fullmatch(record):
                cursor = record_index + 1
                continue
            if _SHELL_MOUNTS_LINE.fullmatch(record):
                cursor = record_index + 1
                continue
            if record == _SHELL_MOUNTS_BEGIN:
                end_index = record_index + 1
                while end_index < len(lines):
                    candidate = lines[end_index].removesuffix(b"\n")
                    if candidate == _SHELL_MOUNTS_END:
                        break
                    if candidate.startswith(_PROTOCOL_PREFIX):
                        raise ProbeProtocolError("invalid protocol inside mounts frame")
                    end_index += 1
                if end_index == len(lines):
                    raise ProbeProtocolError("probe mounts end marker is missing")
                cursor = end_index + 1
                continue
            begin = _DMESG_BEGIN.fullmatch(record)
            if begin is not None:
                _require_nonce(begin, nonce_bytes)
                announced = int(begin.group(2))
                if announced > _MAX_DMESG_BYTES:
                    raise ProbeProtocolError("probe dmesg frame exceeds the byte cap")
                end_index = record_index + 1
                while end_index < len(lines):
                    candidate = lines[end_index].removesuffix(b"\n")
                    if _DMESG_END.fullmatch(candidate) is not None:
                        break
                    if candidate.startswith(_PROTOCOL_PREFIX):
                        raise ProbeProtocolError("invalid protocol inside dmesg frame")
                    end_index += 1
                if end_index == len(lines):
                    raise ProbeProtocolError("probe dmesg end marker is missing")
                end = _DMESG_END.fullmatch(lines[end_index].removesuffix(b"\n"))
                assert end is not None
                _require_nonce(end, nonce_bytes)
                if len(b"".join(lines[record_index + 1 : end_index])) != announced:
                    raise ProbeProtocolError("probe dmesg byte count does not match")
                cursor = end_index + 1
                continue
            raise ProbeProtocolError("probe shell record is out of order")
    reboot_index, reboot_record = _next_protocol_line(lines, cursor)
    reboot = _REBOOT_READY.fullmatch(reboot_record)
    if reboot is None:
        raise ProbeProtocolError("probe reboot readiness is missing")
    _require_nonce(reboot, nonce_bytes)
    for line in lines[reboot_index + 1 :]:
        if line.removesuffix(b"\n").startswith(_PROTOCOL_PREFIX):
            raise ProbeProtocolError(
                "probe transcript contains replayed terminal records"
            )
    return ProbeExchange(tuple(outcomes), status, dmesg)


def probe_bootargs(plan: DebugPlan, session_seconds: int) -> str:
    """Derive the minimal probe boot without carrying external workloads."""

    plan.validate()
    session_seconds = validate_session_seconds(session_seconds)
    tokens = plan.bootargs.split()
    if "--" in tokens:
        tokens = tokens[: tokens.index("--")]
    removed_prefixes = (
        "console=",
        "loglevel=",
        "asterinas.klog_capture=",
        "asterinas.reboot_after=",
        "asterinas.net=",
        "asterinas.neighbor=",
        "systemd.",
    )
    retained = [
        token
        for token in tokens
        if token.partition("=")[0].replace("-", "_") != "asterinas.mmc_write_partition2"
        and token != "init=/init"
        and not token.startswith(removed_prefixes)
    ]
    suffix = (
        "console=ttyS0",
        "loglevel=info",
        "asterinas.klog_capture=info",
        "init=/init",
        f"asterinas.reboot_after={session_seconds}",
        "--",
        "--root-init=probe",
    )
    return " ".join((*retained, *suffix))


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("probe guest deadline expired")
    return remaining


def run_probe(
    bundle: ProbeBundle,
    selected: tuple[ProbeDefinition, ...],
    config: ProbeRunConfig,
    operations: ProbeOperations,
    publisher: ProbePublisher,
    *,
    clock: Callable[[], float] = time.monotonic,
    nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
) -> ProbeRunResult:
    """Run one probe batch and always classify post-boot recovery."""

    publisher.invalidate()
    bundle.validate()
    config.__post_init__()
    if tuple(validate_probe_names(tuple(item.name for item in selected))) != selected:
        raise ProbeContractError("selected probes do not match the registry")
    nonce = _validate_nonce(nonce_factory())
    started = clock()
    exchange: ProbeExchange | None = None
    recovered = False
    reason = "probe-not-started"
    terminal_exchange = False
    phase = "open"
    guest_deadline: float | None = None
    try:
        operations.open(config.open_seconds)
        phase = "artifacts"
        operations.ensure_artifacts(config.artifact_seconds)
        guest_deadline = clock() + config.session_seconds
        phase = "boot"
        operations.boot(
            probe_bootargs(bundle.plan, config.session_seconds),
            _remaining(guest_deadline, clock),
        )
        phase = "exchange"
        exchange = operations.exchange(
            nonce,
            selected,
            config.shell,
            _remaining(guest_deadline, clock),
        )
        terminal_exchange = True
        reason = "probe-pass" if exchange.passed else "probe-failed"
        phase = "reboot-request"
        operations.request_reboot(nonce, _remaining(guest_deadline, clock))
    except (OSError, RuntimeError, TimeoutError, ValueError, BufferError, EOFError):
        reason = f"probe-{phase}-failed"
    finally:
        if operations.guest_started:
            try:
                remaining_guest = (
                    max(0.0, guest_deadline - clock())
                    if guest_deadline is not None
                    else 0.0
                )
                operations.await_recovery(config.recovery_seconds + remaining_guest)
                recovered = True
            except (
                OSError,
                RuntimeError,
                TimeoutError,
                ValueError,
                BufferError,
                EOFError,
            ):
                recovered = False
                reason = "manual-reset-required"
        try:
            transcript = operations.transcript
        except (OSError, RuntimeError, ValueError, BufferError, EOFError) as error:
            transcript = f"serial transcript unavailable: {error}\n".encode(
                "utf-8", errors="replace"
            )
            reason = "manual-reset-required"
            recovered = False
        try:
            operations.close()
        except (OSError, RuntimeError, ValueError, BufferError, EOFError):
            reason = "manual-reset-required"
            recovered = False

    if reason == "probe-pass" and any(
        marker in transcript.lower() for marker in _FATAL_REBOOT_MARKERS
    ):
        reason = "probe-fatal-diagnostics"

    outcomes = exchange.outcomes if exchange is not None else ()
    dmesg = exchange.dmesg if exchange is not None else b""
    result = ProbeRunResult(
        schema_version=1,
        passed=bool(
            terminal_exchange
            and exchange
            and exchange.passed
            and recovered
            and reason == "probe-pass"
        ),
        reason=reason,
        bundle_sha256=bundle.bundle_sha256,
        plan_sha256=bundle.plan_sha256,
        selected_probes=tuple(item.name for item in selected),
        outcomes=outcomes,
        elapsed_seconds=round(max(0.0, clock() - started), 3),
        recovered=recovered,
    )
    publisher.publish(result, transcript, dmesg)
    return result


def _prepare_output_directory(path: Path) -> Path:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProbeContractError("probe output path contains an unsafe component")
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate.chmod(0o700)
    return candidate


def _prepare_bundle_directory(path: Path) -> Path:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProbeContractError("probe bundle path contains an unsafe component")
    if not candidate.exists():
        candidate.mkdir(mode=0o700, parents=True)
    return candidate


def _serial_summary(transcript: bytes) -> bytes:
    if not isinstance(transcript, bytes):
        raise ProbeContractError("serial transcript must be bytes")
    protocol = b"".join(
        line
        for line in transcript.splitlines(keepends=True)
        if line.lstrip(b"\r").startswith(_PROTOCOL_PREFIX)
    )
    context = (
        transcript
        if len(transcript) <= 2 * _SERIAL_CONTEXT_BYTES
        else (
            transcript[:_SERIAL_CONTEXT_BYTES]
            + b"\n[serial context truncated]\n"
            + transcript[-_SERIAL_CONTEXT_BYTES:]
        )
    )
    summary = context + (b"\n[protocol records]\n" + protocol if protocol else b"")
    if len(summary) <= _SERIAL_SUMMARY_BYTES:
        return summary
    marker = b"\n[serial summary truncated]\n"
    head = (_SERIAL_SUMMARY_BYTES - len(marker)) // 2
    tail = _SERIAL_SUMMARY_BYTES - len(marker) - head
    return summary[:head] + marker + summary[-tail:]


class RealProbePublisher:
    """Publish a compact private evidence set with the result last."""

    OUTPUT_NAMES = (
        "result.json",
        "serial-summary.log",
        "failure.dmesg.log",
        "sha256sums.txt",
    )

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = _prepare_output_directory(output_directory)
        self._output = PinnedOutputDirectory(self.output_directory)
        try:
            self._output.lock_exclusive()
        except RuntimeError as error:
            self._output.close()
            raise ProbeContractError("probe output run is already active") from error

    def close(self) -> None:
        self._output.close()

    def invalidate(self) -> None:
        self._output.invalidate(*self.OUTPUT_NAMES)

    def publish(self, result: ProbeRunResult, transcript: bytes, dmesg: bytes) -> None:
        summary = _serial_summary(transcript)
        if len(dmesg) > _MAX_DMESG_BYTES:
            raise ProbeContractError("failure dmesg exceeds the byte cap")
        retained: list[tuple[str, bytes]] = [("serial-summary.log", summary)]
        if not result.passed:
            retained.append(
                (
                    "failure.dmesg.log",
                    dmesg or b"no guest dmesg frame was available\n",
                )
            )
        output = self._output
        output.invalidate("result.json")
        for name, contents in retained:
            output.atomic_write(name, contents, mode=0o600)
        result_payload = result.canonical_bytes()
        summed = (*retained, ("result.json", result_payload))
        sums = "".join(
            f"{hashlib.sha256(contents).hexdigest()}  {name}\n"
            for name, contents in summed
        ).encode()
        output.atomic_write("sha256sums.txt", sums, mode=0o600)
        output.atomic_write("result.json", result_payload, mode=0o600)


def _deadline(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ProbeContractError("operation timeout must be positive")
    return time.monotonic() + timeout


def _wait_for_complete_record(
    serial: SerialConsole,
    record: bytes,
    deadline: float,
    *,
    start: int = 0,
) -> None:
    """Wait through the line ending so classification never sees a partial record."""

    serial.wait_for(record, deadline, start=start)
    record_start = serial.transcript.find(record, start)
    if record_start < 0:
        raise ProbeProtocolError("serial marker disappeared from the transcript")
    serial.wait_for(b"\n", deadline, start=record_start + len(record))


def _wait_for_probe_terminals(
    serial: SerialConsole,
    nonce: str,
    selected: tuple[ProbeDefinition, ...],
    cursor: int,
    deadline: float,
) -> int:
    """Apply each registry deadline while retaining the global guest deadline."""

    for sequence, definition in enumerate(selected):
        probe_deadline = min(deadline, time.monotonic() + definition.timeout_seconds)
        start_record = (
            f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq={sequence} "
            f"name={definition.name}"
        ).encode()
        _wait_for_complete_record(serial, start_record, probe_deadline, start=cursor)
        start_index = serial.transcript.find(start_record, cursor)
        cursor = serial.transcript.find(b"\n", start_index) + 1
        passed = (
            f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq={sequence} "
            f"name={definition.name} detail="
        ).encode()
        failed = (
            f"ASTERINAS_PROBE_FAIL v=1 nonce={nonce} seq={sequence} "
            f"name={definition.name} errno="
        ).encode()
        serial.wait_for_any((passed, failed), probe_deadline, start=cursor)
        candidates = tuple(
            (position, marker)
            for marker in (passed, failed)
            if (position := serial.transcript.find(marker, cursor)) >= 0
        )
        if not candidates:
            raise ProbeProtocolError("probe terminal marker disappeared")
        terminal_index, terminal = min(candidates, key=lambda item: item[0])
        _wait_for_complete_record(serial, terminal, probe_deadline, start=cursor)
        cursor = serial.transcript.find(b"\n", terminal_index) + 1
        if terminal == failed:
            break
    return cursor


class PhysicalProbeOperations:
    """One descriptor-owned minimal U-Boot and Stage1 probe session."""

    def __init__(
        self,
        bundle: ProbeBundle,
        *,
        open_device: Callable[[str], int] = open_serial,
        lock_device: Callable[[int], None] = _lock_serial,
        close_device: Callable[[int], None] = os.close,
        session_factory: Callable[..., BoardSession] = BoardSession.from_fd,
        serial_factory: Callable[..., SerialConsole] = SerialConsole,
    ) -> None:
        bundle.validate()
        self._bundle = bundle
        self._open_device = open_device
        self._lock_device = lock_device
        self._close_device = close_device
        self._session_factory = session_factory
        self._serial_factory = serial_factory
        self._fd: int | None = None
        self._session: BoardSession | None = None
        self._serial: SerialConsole | None = None
        self._log = io.StringIO()
        self._guest_started = False
        self._guest_recovered_early = False
        self._recovery_cursor = 0

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    @property
    def transcript(self) -> bytes:
        serial = self._serial.transcript if self._serial is not None else b""
        return self._log.getvalue().encode("utf-8", errors="replace") + serial

    def _require_session(self) -> BoardSession:
        if self._session is None:
            raise ProbeContractError("physical probe session is not open")
        return self._session

    def _require_serial(self) -> SerialConsole:
        if self._serial is None:
            raise ProbeContractError("physical probe guest is not running")
        return self._serial

    def open(self, timeout: float) -> None:
        deadline = _deadline(timeout)
        fd = self._open_device(self._bundle.device)
        try:
            self._lock_device(fd)
            session = self._session_factory(
                fd,
                None,
                confirm=False,
                log_stream=self._log,
            )
            session.send("")
            session.wait_for_uboot_prompt(
                timeout=max(0.001, deadline - time.monotonic())
            )
        except BaseException:
            self._close_device(fd)
            raise
        self._fd = fd
        self._session = session

    def ensure_artifacts(self, timeout: float) -> tuple[str, ...]:
        session = self._require_session()
        deadline = _deadline(timeout)
        session.command("mmc dev 1", timeout=max(0.001, deadline - time.monotonic()))
        session.command("mmc rescan", timeout=max(0.001, deadline - time.monotonic()))
        identities = {item.name: item for item in self._bundle.plan.artifacts}
        outcomes = []
        for artifact in self._bundle.mmc_artifacts:
            identity = identities[artifact.name]
            actual_size = session.load_artifact(
                artifact.name,
                artifact.path,
                identity.load_address,
                identity.crc32,
            )
            if actual_size != identity.size:
                raise ProbeContractError(
                    f"{artifact.name}: MMC size mismatch: expected "
                    f"{identity.size}, got {actual_size}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError("MMC artifact verification deadline expired")
            outcomes.append(f"{artifact.name}:mmc")
        return tuple(outcomes)

    def boot(self, bootargs: str, timeout: float) -> None:
        session = self._require_session()
        if self._fd is None:
            raise ProbeContractError("physical serial descriptor is unavailable")
        deadline = _deadline(timeout)
        identities = {item.name: item for item in self._bundle.plan.artifacts}
        initramfs = identities["initramfs"]
        dtb = identities["megrez_dtb"]
        commands = (
            f"fdt addr 0x{dtb.load_address:x}",
            f"setenv initrd_size 0x{initramfs.size:x}",
            *_uboot_bootargs_commands(bootargs),
        )
        for command in commands:
            session.command(command, timeout=max(0.001, deadline - time.monotonic()))
        kernel = identities["kernel"]
        session.start_boot_attempt()
        self._guest_started = True
        session.send(
            f"booti 0x{kernel.load_address:x} "
            f"0x{initramfs.load_address:x}:0x{initramfs.size:x} "
            f"0x{dtb.load_address:x}"
        )
        self._serial = self._serial_factory(
            self._fd,
            max_bytes=_PHYSICAL_TRANSCRIPT_BYTES,
            tx_delay=0.005,
        )

    def _interactive_shell(
        self, serial: SerialConsole, deadline: float, nonce: str | None = None
    ) -> None:
        if nonce is not None:
            ready_record = (
                f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={_validate_nonce(nonce)}"
            ).encode()
            serial.wait_for(ready_record, deadline)
        else:
            serial.wait_for(_SHELL_READY_PREFIX, deadline)
        if nonce is None:
            matches = tuple(_SHELL_READY.finditer(serial.transcript))
            if len(matches) != 1:
                raise ProbeProtocolError("probe shell nonce is ambiguous")
            nonce = matches[0].group(1).decode()
            recovery_cursor = matches[0].end()
        else:
            ready_position = serial.transcript.find(ready_record)
            if ready_position < 0:
                raise ProbeProtocolError("probe shell readiness is invalid")
            recovery_cursor = ready_position + len(ready_record)
        input_fd = sys.stdin.fileno()
        input_flags = fcntl.fcntl(input_fd, fcntl.F_GETFL)
        fcntl.fcntl(input_fd, fcntl.F_SETFL, input_flags | os.O_NONBLOCK)
        input_buffer = bytearray()
        discard_input = False

        def reject_recovered_guest() -> None:
            if any(
                serial.transcript.find(marker, recovery_cursor) >= 0
                for marker in (b"OpenSBI v", b"U-Boot ")
            ):
                self._recovery_cursor = recovery_cursor
                self._guest_recovered_early = True
                raise ProbeProtocolError("guest rebooted during probe shell")

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("bounded probe shell expired")
                reject_recovered_guest()
                if b"\n" not in input_buffer:
                    sys.stdout.write("probe> ")
                    sys.stdout.flush()
                    watched = [input_fd]
                    serial_fd = getattr(serial, "fd", None)
                    if isinstance(serial_fd, int) and serial_fd >= 0:
                        watched.append(serial_fd)
                    readable, _, _ = select.select(watched, [], [], remaining)
                    if not readable:
                        raise TimeoutError("bounded probe shell expired")
                    if serial_fd in readable:
                        try:
                            serial.wait_for_any(
                                (b"OpenSBI v", b"U-Boot "),
                                min(deadline, time.monotonic() + 0.05),
                                start=recovery_cursor,
                            )
                        except TimeoutError:
                            continue
                        reject_recovered_guest()
                    if input_fd not in readable:
                        continue
                    try:
                        chunk = os.read(input_fd, 514)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        input_buffer.clear()
                        input_buffer.extend(b"exit\n")
                    else:
                        input_buffer.extend(chunk)

                if discard_input:
                    newline = input_buffer.find(b"\n")
                    if newline < 0:
                        input_buffer.clear()
                    else:
                        del input_buffer[: newline + 1]
                        discard_input = False
                    continue
                newline = input_buffer.find(b"\n")
                if newline < 0:
                    if len(input_buffer) > 512:
                        input_buffer.clear()
                        discard_input = True
                        print("allowed commands: " + ", ".join(sorted(_SHELL_COMMANDS)))
                    continue
                raw_line = bytes(input_buffer[:newline]).removesuffix(b"\r")
                del input_buffer[: newline + 1]
                try:
                    command = raw_line.decode("ascii")
                except UnicodeDecodeError:
                    command = ""
                if len(raw_line) > 512 or command not in _SHELL_COMMANDS:
                    print("allowed commands: " + ", ".join(sorted(_SHELL_COMMANDS)))
                    continue
                reject_recovered_guest()
                cursor = serial.checkpoint()
                serial.send((command + "\n").encode(), deadline)
                if command == "help":
                    terminals = (_SHELL_COMMANDS_RECORD,)
                elif command == "dmesg":
                    terminals = (
                        f"ASTERINAS_PROBE_DMESG_END v=1 nonce={nonce}".encode(),
                    )
                elif command == "mounts":
                    terminals = (
                        _SHELL_MOUNTS_END,
                        b"ASTERINAS_PROBE_SHELL_MOUNTS proc /proc proc",
                        b"ASTERINAS_PROBE_SHELL_REJECT reason=mounts-unavailable",
                    )
                elif command == "exit":
                    terminals = (
                        f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}".encode(),
                    )
                else:
                    terminals = (
                        f"ASTERINAS_PROBE_SHELL_RESULT name={command} ".encode(),
                    )
                serial.wait_for_any(terminals, deadline, start=cursor)
                terminal_positions = tuple(
                    (position, marker)
                    for marker in terminals
                    if (position := serial.transcript.find(marker, cursor)) >= 0
                )
                if not terminal_positions:
                    raise ProbeProtocolError("probe shell response disappeared")
                terminal_index, terminal = min(
                    terminal_positions, key=lambda item: item[0]
                )
                _wait_for_complete_record(serial, terminal, deadline, start=cursor)
                end = serial.transcript.find(b"\n", terminal_index) + 1
                sys.stdout.write(
                    serial.transcript[cursor:end].decode("utf-8", errors="replace")
                )
                sys.stdout.flush()
                if command == "exit":
                    return
        finally:
            fcntl.fcntl(input_fd, fcntl.F_SETFL, input_flags)

    @staticmethod
    def _classification_transcript(transcript: bytes, nonce: str) -> bytes:
        _validate_nonce(nonce)
        return transcript

    def exchange(
        self,
        nonce: str,
        selected: tuple[ProbeDefinition, ...],
        shell: bool,
        timeout: float,
    ) -> ProbeExchange:
        serial = self._require_serial()
        deadline = _deadline(timeout)
        serial.wait_for(_PROBE_READY, deadline)
        cursor = serial.checkpoint()
        serial.send(encode_probe_request(nonce, selected, shell=shell), deadline)
        cursor = _wait_for_probe_terminals(serial, nonce, selected, cursor, deadline)
        if shell:
            self._interactive_shell(serial, deadline, nonce)
        reboot_ready = f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}".encode()
        _wait_for_complete_record(serial, reboot_ready, deadline, start=cursor)
        evidence = self._classification_transcript(serial.transcript, nonce)
        return classify_probe_transcript(
            evidence,
            nonce,
            tuple(item.name for item in selected),
            shell=shell,
        )

    def request_reboot(self, nonce: str, timeout: float) -> None:
        if self._guest_recovered_early:
            return
        serial = self._require_serial()
        deadline = _deadline(timeout)
        self._recovery_cursor = serial.checkpoint()
        serial.send(
            f"ASTERINAS_PROBE_REBOOT v=1 nonce={_validate_nonce(nonce)}\n".encode(),
            deadline,
        )

    def await_recovery(self, timeout: float) -> None:
        serial = self._require_serial()
        deadline = _deadline(timeout)
        serial.wait_for(b"U-Boot ", deadline, start=self._recovery_cursor)
        serial.send(b"\n", deadline)
        serial.wait_for(b"=> ", deadline, start=self._recovery_cursor)
        recovery = serial.transcript[self._recovery_cursor :].decode(
            "utf-8", errors="replace"
        )
        validate_recovery_epoch(recovery)

    def close(self) -> None:
        if self._fd is not None:
            self._close_device(self._fd)
            self._fd = None
        self._session = None
        self._serial = None
        self._log.close()


def qemu_probe_argv(
    kernel: Path,
    initramfs: Path,
    bootargs: str,
) -> tuple[str, ...]:
    """Build the device-free QEMU command for one Stage1 probe boot."""

    if not isinstance(kernel, Path) or not isinstance(initramfs, Path):
        raise ProbeContractError("QEMU artifact paths must be Path values")
    if not isinstance(bootargs, str) or not bootargs:
        raise ProbeContractError("QEMU probe bootargs must be nonempty")
    return (
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-cpu",
        GENERIC_SV39_CPU,
        "-m",
        "2G",
        "-smp",
        "4",
        "-nographic",
        "-nic",
        "none",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-initrd",
        str(initramfs),
        "-append",
        bootargs,
    )


class QemuProbeOperations:
    """Run the same Stage1 protocol in QEMU without disks or network devices."""

    def __init__(
        self,
        bundle: ProbeBundle,
        kernel: Path,
        initramfs: Path,
        *,
        launch: Callable[..., GateProcess] = launch_process,
        serial_factory: Callable[..., SerialConsole] = SerialConsole,
        openpty: Callable[[], tuple[int, int]] = os.openpty,
    ) -> None:
        bundle.validate()
        self._bundle = bundle
        self._paths = {"kernel": Path(kernel), "initramfs": Path(initramfs)}
        self._launch = launch
        self._serial_factory = serial_factory
        self._openpty = openpty
        self._artifact_fds: dict[str, int] = {}
        self._master_fd = -1
        self._process: GateProcess | None = None
        self._serial: SerialConsole | None = None
        self._guest_started = False

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    @property
    def transcript(self) -> bytes:
        return self._serial.transcript if self._serial is not None else b""

    def _require_serial(self) -> SerialConsole:
        if self._serial is None:
            raise ProbeContractError("QEMU probe guest is not running")
        return self._serial

    def open(self, timeout: float) -> None:
        deadline = _deadline(timeout)
        if self._artifact_fds:
            raise ProbeContractError("QEMU probe session is already open")
        try:
            for name in ("kernel", "initramfs"):
                if time.monotonic() >= deadline:
                    raise TimeoutError("QEMU artifact open deadline expired")
                descriptor = os.open(
                    self._paths[name],
                    os.O_RDONLY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC
                    | getattr(os, "O_NONBLOCK", 0),
                )
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
                    os.close(descriptor)
                    raise ProbeContractError(
                        f"{name}: QEMU artifact must be a nonempty regular file"
                    )
                self._artifact_fds[name] = descriptor
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity_from_fd(descriptor: int, maximum: int) -> tuple[int, str, str]:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise ProbeContractError("QEMU artifact is outside the bounded size")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        crc = 0
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, maximum + 1 - size)):
            size += len(chunk)
            if size > maximum:
                raise ProbeContractError("QEMU artifact exceeds the size cap")
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
        if size != metadata.st_size:
            raise ProbeContractError("QEMU artifact changed while being verified")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return size, digest.hexdigest(), f"{crc:08x}"

    def ensure_artifacts(self, timeout: float) -> tuple[str, ...]:
        deadline = _deadline(timeout)
        identities = {item.name: item for item in self._bundle.plan.artifacts}
        if set(self._artifact_fds) != {"kernel", "initramfs"}:
            raise ProbeContractError("QEMU probe session is not open")
        outcomes = []
        for name in ("kernel", "initramfs"):
            if time.monotonic() >= deadline:
                raise TimeoutError("QEMU artifact verification deadline expired")
            identity = identities[name]
            observed = self._identity_from_fd(
                self._artifact_fds[name], 64 * 1024 * 1024
            )
            expected = (identity.size, identity.sha256, identity.crc32)
            if observed != expected:
                raise ProbeContractError(f"{name}: QEMU artifact identity mismatch")
            outcomes.append(f"{name}:qemu")
        return tuple(outcomes)

    def boot(self, bootargs: str, timeout: float) -> None:
        _deadline(timeout)
        if self._process is not None or self._serial is not None:
            raise ProbeContractError("QEMU probe guest is already running")
        if set(self._artifact_fds) != {"kernel", "initramfs"}:
            raise ProbeContractError("QEMU probe session is not open")
        kernel = Path(f"/proc/self/fd/{self._artifact_fds['kernel']}")
        initramfs = Path(f"/proc/self/fd/{self._artifact_fds['initramfs']}")
        master, slave = self._openpty()
        process: GateProcess | None = None
        try:
            tty.setraw(slave)
            process = self._launch(
                qemu_probe_argv(kernel, initramfs, bootargs),
                stdio_fd=slave,
                pass_fds=tuple(self._artifact_fds.values()),
            )
            os.close(slave)
            slave = -1
            self._master_fd = master
            self._process = process
            self._serial = self._serial_factory(
                master, process=process, max_bytes=_MAX_TRANSCRIPT_BYTES
            )
            self._guest_started = True
        except BaseException:
            if slave >= 0:
                os.close(slave)
            if process is not None:
                now = time.monotonic()
                process.terminate_group(now + 1, now + 2)
            os.close(master)
            raise

    def exchange(
        self,
        nonce: str,
        selected: tuple[ProbeDefinition, ...],
        shell: bool,
        timeout: float,
    ) -> ProbeExchange:
        if shell:
            raise ProbeContractError("the bounded diagnostic shell is physical-only")
        serial = self._require_serial()
        deadline = _deadline(timeout)
        serial.wait_for(_PROBE_READY, deadline)
        cursor = serial.checkpoint()
        serial.send(encode_probe_request(nonce, selected), deadline)
        cursor = _wait_for_probe_terminals(serial, nonce, selected, cursor, deadline)
        reboot_ready = f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}".encode()
        _wait_for_complete_record(serial, reboot_ready, deadline, start=cursor)
        return classify_probe_transcript(
            serial.transcript, nonce, tuple(item.name for item in selected)
        )

    def await_ready(self, timeout: float) -> None:
        self._require_serial().wait_for(_PROBE_READY, _deadline(timeout))

    def request_reboot(self, nonce: str, timeout: float) -> None:
        serial = self._require_serial()
        serial.send(
            f"ASTERINAS_PROBE_REBOOT v=1 nonce={_validate_nonce(nonce)}\n".encode(),
            _deadline(timeout),
        )

    def await_recovery(self, timeout: float) -> None:
        if self._process is None:
            raise ProbeContractError("QEMU probe process is unavailable")
        deadline = _deadline(timeout)
        serial = self._require_serial()
        while self._process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("QEMU probe recovery deadline expired")
            serial.drain(min(deadline, now + 0.05))
        returncode = self._process.wait(deadline)
        if time.monotonic() < deadline:
            serial.drain(min(deadline, time.monotonic() + 1.0))
        if returncode != 0:
            raise RuntimeError(f"QEMU probe exited with status {returncode}")

    def close(self) -> None:
        if self._process is not None and self._process.poll() is None:
            now = time.monotonic()
            self._process.terminate_group(now + 1, now + 2)
        self._process = None
        self._serial = None
        if self._master_fd >= 0:
            os.close(self._master_fd)
            self._master_fd = -1
        for descriptor in self._artifact_fds.values():
            os.close(descriptor)
        self._artifact_fds.clear()


def run_qemu_deadline_gate(
    bundle: ProbeBundle,
    operations: QemuProbeOperations,
    publisher: ProbePublisher,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeRunResult:
    """Prove that the fixed 30-second kernel timer exits QEMU without input."""

    publisher.invalidate()
    bundle.validate()
    started = clock()
    deadline = started + 45
    ready = False
    recovered = False
    reason = "deadline-not-started"
    boot_started: float | None = None
    try:
        operations.open(_remaining(deadline, clock))
        operations.ensure_artifacts(_remaining(deadline, clock))
        boot_started = clock()
        operations.boot(probe_bootargs(bundle.plan, 30), _remaining(deadline, clock))
        operations.await_ready(_remaining(deadline, clock))
        ready = True
        operations.await_recovery(_remaining(deadline, clock))
        recovered = True
        transcript = operations.transcript
        lowered = transcript.lower()
        elapsed_from_boot = clock() - boot_started
        if (
            transcript.count(_SOFTWARE_REBOOT_ARMED_30) != 1
            or any(marker in lowered for marker in _FATAL_REBOOT_MARKERS)
            or elapsed_from_boot < _MIN_DEADLINE_REBOOT_SECONDS
        ):
            raise ProbeProtocolError("QEMU exit did not prove the reboot deadline")
        reason = "deadline-reboot-pass"
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        BufferError,
        EOFError,
    ):
        reason = (
            "deadline-failed"
            if recovered or not operations.guest_started
            else "manual-reset-required"
        )
    finally:
        try:
            transcript = operations.transcript
        except (OSError, RuntimeError, ValueError, BufferError, EOFError) as error:
            transcript = f"serial transcript unavailable: {error}\n".encode(
                "utf-8", errors="replace"
            )
            reason = "manual-reset-required"
            recovered = False
        try:
            operations.close()
        except (OSError, RuntimeError, ValueError, BufferError, EOFError):
            reason = "manual-reset-required"
            recovered = False
    result = ProbeRunResult(
        schema_version=1,
        passed=ready and recovered and reason == "deadline-reboot-pass",
        reason=reason,
        bundle_sha256=bundle.bundle_sha256,
        plan_sha256=bundle.plan_sha256,
        selected_probes=(),
        outcomes=(),
        elapsed_seconds=round(max(0.0, clock() - started), 3),
        recovered=recovered,
    )
    publisher.publish(result, transcript, b"")
    return result


def _read_bounded_regular(path: Path, maximum: int, label: str) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise ProbeContractError(f"{label} is not a bounded regular file")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size:
            raise ProbeContractError(f"{label} changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _session_seconds_argument(value: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("session seconds must be a canonical integer")
    try:
        return validate_session_seconds(int(value))
    except ProbeContractError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parse_args(arguments: Sequence[str]) -> argparse.Namespace:
    """Parse the one-time configure path or the normal name-only probe path."""

    if list(arguments[:1]) == ["configure"]:
        parser = argparse.ArgumentParser(
            description="Select the current Megrez probe bundle"
        )
        parser.add_argument("action", choices=("configure",))
        parser.add_argument("--plan", required=True, type=Path)
        parser.add_argument("--device", required=True)
        parser.add_argument("--mmc-kernel", required=True)
        parser.add_argument("--mmc-initramfs", required=True)
        parser.add_argument("--mmc-dtb", required=True)
        parser.add_argument(
            "--bundle", type=Path, default=Path("target/megrez-probe/current.json")
        )
        return parser.parse_args(arguments)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probes", nargs="+", choices=tuple(_PROBE_REGISTRY))
    parser.add_argument(
        "--bundle", type=Path, default=Path("target/megrez-probe/current.json")
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("target/megrez-probe/latest"),
    )
    parser.add_argument("--session-seconds", type=_session_seconds_argument, default=90)
    parser.add_argument("--recovery-seconds", type=int, default=30)
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--qemu", action="store_true")
    parser.add_argument("--qemu-kernel", type=Path)
    parser.add_argument("--qemu-initramfs", type=Path)
    parser.add_argument("--qemu-deadline-only", action="store_true")
    values = parser.parse_args(arguments)
    qemu_paths = values.qemu_kernel is not None and values.qemu_initramfs is not None
    if values.qemu and not qemu_paths:
        parser.error("--qemu requires --qemu-kernel and --qemu-initramfs")
    if not values.qemu and (
        values.qemu_kernel is not None
        or values.qemu_initramfs is not None
        or values.qemu_deadline_only
    ):
        parser.error("QEMU options require --qemu")
    if values.qemu and values.shell:
        parser.error("--qemu cannot be combined with the physical-only --shell")
    values.action = "run"
    values.probes = tuple(values.probes)
    return values


def _configure(values: argparse.Namespace) -> ProbeBundle:
    plan = DebugPlan.from_bytes(_read_bounded_regular(values.plan, 64 * 1024, "plan"))
    bundle = ProbeBundle(
        schema_version=1,
        plan=plan,
        plan_sha256=plan.plan_sha256,
        device=values.device,
        mmc_artifacts=(
            MmcArtifact("kernel", values.mmc_kernel),
            MmcArtifact("initramfs", values.mmc_initramfs),
            MmcArtifact("megrez_dtb", values.mmc_dtb),
        ),
    )
    bundle.validate()
    directory = _prepare_bundle_directory(values.bundle.parent)
    with PinnedOutputDirectory(directory) as output:
        output.atomic_write(values.bundle.name, bundle.canonical_bytes(), mode=0o600)
    return bundle


def main(
    arguments: Sequence[str] | None = None,
    *,
    operations_factory: Callable[
        [ProbeBundle], ProbeOperations
    ] = PhysicalProbeOperations,
    qemu_operations_factory: Callable[
        [ProbeBundle, Path, Path], QemuProbeOperations
    ] = QemuProbeOperations,
    stdin_isatty: Callable[[], bool] = sys.stdin.isatty,
) -> int:
    """Configure or execute the current one-command physical probe loop."""

    values = parse_args(tuple(sys.argv[1:] if arguments is None else arguments))
    publisher: RealProbePublisher | None = None
    try:
        if values.action == "configure":
            bundle = _configure(values)
            print(f"MEGREZ_PROBE_CONFIGURED bundle_sha256={bundle.bundle_sha256}")
            return 0
        publisher = RealProbePublisher(values.output_directory)
        publisher.invalidate()
        if values.shell and not stdin_isatty():
            raise ProbeContractError("--shell requires an interactive terminal")
        bundle = ProbeBundle.from_bytes(
            _read_bounded_regular(values.bundle, 128 * 1024, "probe bundle")
        )
        if values.qemu:
            operations = qemu_operations_factory(
                bundle, values.qemu_kernel, values.qemu_initramfs
            )
            if values.qemu_deadline_only:
                result = run_qemu_deadline_gate(bundle, operations, publisher)
            else:
                result = run_probe(
                    bundle,
                    validate_probe_names(values.probes),
                    ProbeRunConfig(
                        session_seconds=values.session_seconds,
                        recovery_seconds=values.recovery_seconds,
                        shell=values.shell,
                    ),
                    operations,
                    publisher,
                )
        else:
            selected = validate_probe_names(values.probes)
            config = ProbeRunConfig(
                session_seconds=values.session_seconds,
                recovery_seconds=values.recovery_seconds,
                shell=values.shell,
            )
            result = run_probe(
                bundle,
                selected,
                config,
                operations_factory(bundle),
                publisher,
            )
    except (DebugContractError, OSError, ProbeContractError, RuntimeError) as error:
        print(f"megrez-probe: {error}", file=sys.stderr)
        return 2
    finally:
        if publisher is not None:
            publisher.close()
    print(result.canonical_bytes().decode(), end="")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
