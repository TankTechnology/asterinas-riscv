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
_MMC_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*")
_SERIAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_SAFE_DETAIL = r"[a-z0-9][a-z0-9-]*"
_START = re.compile(
    rb"ASTERINAS_PROBE_START v=1 nonce=([0-9a-f]{32}) seq=([0-9]+) "
    rb"name=([a-z0-9-]+)"
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
_PROTOCOL_NONCE = re.compile(rb"ASTERINAS_PROBE_[^\r\n]*nonce=([0-9a-f]{32})")
_PROTOCOL_PREFIX = b"ASTERINAS_PROBE_"
_READY = b"ASTERINAS_PROBE_READY v=1 pid=1"
_MAX_TRANSCRIPT_BYTES = 256 * 1024
_MAX_DMESG_BYTES = 32 * 1024


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
    transcript: bytes, nonce: str, selected: Sequence[str]
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
        _START,
        _PASS,
        _FAIL,
        _DMESG_BEGIN,
        _DMESG_END,
        _DONE,
        _REBOOT_READY,
    )
    for line in lines:
        record = line.removesuffix(b"\n")
        if (
            record.startswith(_PROTOCOL_PREFIX)
            and record != _READY
            and not any(pattern.fullmatch(record) for pattern in known_patterns)
        ):
            unknown.append(record)
    if unknown:
        raise ProbeProtocolError("probe transcript contains an unknown protocol marker")

    ready_index = next(
        index for index, line in enumerate(lines) if line.removesuffix(b"\n") == _READY
    )
    cursor = ready_index + 1
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
    reboot_index, reboot_record = _next_protocol_line(lines, done_index + 1)
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
