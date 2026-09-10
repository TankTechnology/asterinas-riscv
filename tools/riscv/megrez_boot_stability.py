#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Prove three unattended Megrez boots from one immutable MMC deployment."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from tools.riscv.debian.rootfs.debug_console_protocol import (
    DEBUG_CONSOLE_READY,
    run_debug_console_phase,
)
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_board_session import (
    TERMINAL_ESCAPE_PATTERN,
    safe_artifact_name,
    validate_debug_console_readiness,
    validate_recovery_epoch,
)
from tools.riscv.megrez_physical_graphics import (
    HostGateError,
    PHYSICAL_REBOOT_AFTER,
    RealPhysicalGraphicsOperations,
    _positive_seconds,
    _read_plan,
    _safe_output_directory,
    physical_bootargs,
)


BOOT_STABILITY_CYCLES = 3
MAX_DIAGNOSTICS_BYTES = 256 * 1024
MAX_DEPLOYMENT_MEASUREMENT_BYTES = 2 * 1024 * 1024
MAX_SERIAL_COMMAND_BYTES = 768
GUEST_STEP_TIMEOUT = 15.0
GUEST_ABORT_TIMEOUT = 3.0
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_MEASUREMENT_NONCE = re.compile(r"\A[0-9a-f]{32}\Z")
_MEASUREMENT_BEGIN = re.compile(
    r"__ASTERINAS_ROCKOS_MEASUREMENT_BEGIN__ nonce=([0-9a-f]{32}) "
    r"plan_sha256=([0-9a-f]{64}) partition=([^ ]+) "
    r"boot_id=([0-9a-f-]+) status=([0-9]+)"
)
_MEASUREMENT_ARTIFACT = re.compile(
    r"__ASTERINAS_ROCKOS_ARTIFACT__ nonce=([0-9a-f]{32}) "
    r"name=([^ ]+) mmc_path=([^ ]+) size=([0-9]+) "
    r"sha256=([0-9a-f]{64}) status=([0-9]+)"
)
_MEASUREMENT_END = re.compile(
    r"__ASTERINAS_ROCKOS_MEASUREMENT_END__ nonce=([0-9a-f]{32}) "
    r"artifacts=([0-9]+) status=([0-9]+)"
)
_MEASUREMENT_NATIVE_STAT = re.compile(r"([0-9]+) /boot/(.+)")
_MEASUREMENT_NATIVE_SHA256 = re.compile(r"([0-9a-f]{64})  /boot/(.+)")
_BOOT_PREFLIGHT = re.compile(
    r"__ASTERINAS_BOOT_PREFLIGHT__ nonce=([0-9a-f]{16}) browser_pid=([0-9]+) "
    r"framebuffer=([01]) xorg_fbdev=([01]) openbox=([01]) firefox=([01]) "
    r"browser_service=([a-z-]+) browser_restarts=([0-9]+)"
)
_DIAGNOSTICS_BEGIN = re.compile(
    r"__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce=([0-9a-f]{16}) "
    r"size=([0-9]+) sha256=([0-9a-f]{64})"
)
_FATAL_DIAGNOSTIC_MARKERS = (
    b"kernel panic",
    b"uncaught panic:",
    b"not syncing",
    b"oops:",
    b"fatal exception",
    b"out of memory",
    b"oom-kill:",
    b"killed process",
    b"ext2-fs error",
    b"buffer i/o error",
    b"blk_update_request: i/o error",
    b"end_request: i/o error",
)


@dataclass(frozen=True)
class BootReadinessEvidence:
    """Input-independent userspace and display state required after boot."""

    browser_pid: int
    framebuffer: bool
    xorg_fbdev: bool
    openbox: bool
    firefox: bool
    browser_service: str
    browser_restarts: int

    def __post_init__(self) -> None:
        if (
            type(self.browser_pid) is not int
            or self.browser_pid <= 1
            or any(
                value is not True
                for value in (
                    self.framebuffer,
                    self.xorg_fbdev,
                    self.openbox,
                    self.firefox,
                )
            )
            or self.browser_service != "active"
            or type(self.browser_restarts) is not int
            or self.browser_restarts != 0
        ):
            raise HostGateError("boot readiness contract is incomplete")


@dataclass(frozen=True)
class BootStabilityConfig:
    """Independent bounded deadlines for one boot-stability cycle."""

    open_timeout: float = 60.0
    artifact_timeout: float = 300.0
    boot_timeout: float = 180.0
    readiness_timeout: float = 240.0
    diagnostics_timeout: float = 60.0
    reboot_timeout: float = 30.0
    recovery_timeout: float = 180.0

    def __post_init__(self) -> None:
        values = (
            self.open_timeout,
            self.artifact_timeout,
            self.boot_timeout,
            self.readiness_timeout,
            self.diagnostics_timeout,
            self.reboot_timeout,
            self.recovery_timeout,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1200
            for value in values
        ):
            raise ValueError("boot-stability deadlines must be in (0, 1200]")


@dataclass(frozen=True)
class BootCycleEvidence:
    """Canonical evidence from one complete boot and recovery epoch."""

    cycle: int
    readiness: BootReadinessEvidence
    transport: tuple[str, ...]
    serial_sha256: str
    diagnostics_sha256: str
    artifact_seconds: float
    readiness_seconds: float
    diagnostics_seconds: float
    recovery_seconds: float
    recovered: bool

    def __post_init__(self) -> None:
        if (
            type(self.cycle) is not int
            or not 1 <= self.cycle <= BOOT_STABILITY_CYCLES
            or not self.transport
            or any(not isinstance(item, str) or not item for item in self.transport)
            or _SHA256.fullmatch(self.serial_sha256) is None
            or _SHA256.fullmatch(self.diagnostics_sha256) is None
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in (
                    self.artifact_seconds,
                    self.readiness_seconds,
                    self.diagnostics_seconds,
                    self.recovery_seconds,
                )
            )
            or self.recovered is not True
        ):
            raise HostGateError("boot cycle evidence is invalid")


@dataclass(frozen=True)
class BootAttemptEvidence:
    """Terminal identity and recovery state for every started boot cycle."""

    cycle: int
    serial_sha256: str
    diagnostics_sha256: str
    recovered: bool

    def __post_init__(self) -> None:
        if (
            type(self.cycle) is not int
            or not 1 <= self.cycle <= BOOT_STABILITY_CYCLES
            or _SHA256.fullmatch(self.serial_sha256) is None
            or _SHA256.fullmatch(self.diagnostics_sha256) is None
            or not isinstance(self.recovered, bool)
        ):
            raise HostGateError("boot attempt evidence is invalid")


@dataclass(frozen=True)
class AttestedArtifact:
    """One RockOS-observed immutable partition-1 artifact identity."""

    name: str
    mmc_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not isinstance(self.mmc_path, str)
            or not isinstance(self.sha256, str)
        ):
            raise HostGateError("deployment artifact attestation is invalid")
        try:
            safe_artifact_name(self.mmc_path)
        except argparse.ArgumentTypeError as error:
            raise HostGateError("deployment attestation MMC path is unsafe") from error
        if (
            self.name not in {"kernel", "initramfs", "megrez_dtb"}
            or type(self.size) is not int
            or self.size <= 0
            or _SHA256.fullmatch(self.sha256) is None
        ):
            raise HostGateError("deployment artifact attestation is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> AttestedArtifact:
        if not isinstance(value, Mapping) or set(value) != {
            "name",
            "mmc_path",
            "size",
            "sha256",
        }:
            raise HostGateError("deployment artifact attestation has invalid fields")
        return cls(
            name=value["name"],
            mmc_path=value["mmc_path"],
            size=value["size"],
            sha256=value["sha256"],
        )


@dataclass(frozen=True)
class DeploymentAttestation:
    """Reusable receipt for SHA-256 values observed by RockOS on partition 1."""

    schema_version: int
    plan_sha256: str
    observer: str
    partition: str
    rockos_boot_id: str
    measurement_nonce: str
    measurement_log_sha256: str
    rockos_recovered: bool
    artifacts: tuple[AttestedArtifact, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan_sha256, str)
            or not isinstance(self.observer, str)
            or not isinstance(self.partition, str)
            or not isinstance(self.rockos_boot_id, str)
            or not isinstance(self.measurement_nonce, str)
            or not isinstance(self.measurement_log_sha256, str)
            or not isinstance(self.artifacts, tuple)
            or any(
                not isinstance(artifact, AttestedArtifact)
                for artifact in self.artifacts
            )
        ):
            raise HostGateError("deployment attestation is invalid")
        names = tuple(artifact.name for artifact in self.artifacts)
        if (
            self.schema_version != 2
            or _SHA256.fullmatch(self.plan_sha256) is None
            or self.observer != "rockos-sha256sum"
            or self.partition != "/dev/mmcblk1p1"
            or _BOOT_ID.fullmatch(self.rockos_boot_id) is None
            or _MEASUREMENT_NONCE.fullmatch(self.measurement_nonce) is None
            or _SHA256.fullmatch(self.measurement_log_sha256) is None
            or self.rockos_recovered is not True
            or names != ("kernel", "initramfs", "megrez_dtb")
        ):
            raise HostGateError("deployment attestation is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> DeploymentAttestation:
        fields = {
            "schema_version",
            "plan_sha256",
            "observer",
            "partition",
            "rockos_boot_id",
            "measurement_nonce",
            "measurement_log_sha256",
            "rockos_recovered",
            "artifacts",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise HostGateError("deployment attestation has invalid fields")
        artifacts = value["artifacts"]
        if not isinstance(artifacts, list):
            raise HostGateError("deployment attestation artifacts must be a list")
        return cls(
            schema_version=value["schema_version"],
            plan_sha256=value["plan_sha256"],
            observer=value["observer"],
            partition=value["partition"],
            rockos_boot_id=value["rockos_boot_id"],
            measurement_nonce=value["measurement_nonce"],
            measurement_log_sha256=value["measurement_log_sha256"],
            rockos_recovered=value["rockos_recovered"],
            artifacts=tuple(AttestedArtifact.from_mapping(item) for item in artifacts),
        )

    @classmethod
    def from_measurement_log(cls, payload: bytes) -> DeploymentAttestation:
        """Build a receipt only from a complete controlled RockOS transcript."""

        normalized, protocol = _parse_measurement_protocol(payload)
        begin, *artifact_matches, end = protocol
        artifacts = tuple(
            AttestedArtifact(
                name=match.group(2),
                mmc_path=match.group(3),
                size=int(match.group(4)),
                sha256=match.group(5),
            )
            for match in artifact_matches
        )
        attestation = cls(
            schema_version=2,
            plan_sha256=begin.group(2),
            observer="rockos-sha256sum",
            partition=begin.group(3),
            rockos_boot_id=begin.group(4),
            measurement_nonce=begin.group(1),
            measurement_log_sha256=hashlib.sha256(payload).hexdigest(),
            rockos_recovered=True,
            artifacts=artifacts,
        )
        attestation._validate_protocol(normalized, protocol)
        return attestation

    def validate(
        self,
        plan: Any,
        mmc_artifacts: Mapping[str, str],
        measurement_log: bytes,
    ) -> None:
        """Fail unless every observed identity matches the selected plan and path."""

        if (
            not isinstance(measurement_log, bytes)
            or not 0 < len(measurement_log) <= MAX_DEPLOYMENT_MEASUREMENT_BYTES
            or hashlib.sha256(measurement_log).hexdigest()
            != self.measurement_log_sha256
        ):
            raise HostGateError("deployment measurement log identity mismatch")
        if self.plan_sha256 != plan.plan_sha256:
            raise HostGateError("deployment attestation plan identity mismatch")
        identities = {identity.name: identity for identity in plan.artifacts}
        if set(mmc_artifacts) != {"kernel", "initramfs", "megrez_dtb"}:
            raise HostGateError("deployment attestation requires three MMC paths")
        for artifact in self.artifacts:
            identity = identities[artifact.name]
            if artifact.mmc_path != mmc_artifacts[artifact.name]:
                raise HostGateError(
                    f"{artifact.name}: deployment attestation MMC path mismatch"
                )
            if artifact.size != identity.size or artifact.sha256 != identity.sha256:
                raise HostGateError(
                    f"{artifact.name}: deployment attestation identity mismatch"
                )
        self._validate_measurement_log(measurement_log)

    def _validate_measurement_log(self, payload: bytes) -> None:
        normalized, protocol = _parse_measurement_protocol(payload)
        self._validate_protocol(normalized, protocol)

    def _validate_protocol(
        self, normalized: str, protocol: list[re.Match[str]]
    ) -> None:
        begin, *artifact_matches, end = protocol
        if (
            begin.group(1) != self.measurement_nonce
            or begin.group(2) != self.plan_sha256
            or begin.group(3) != self.partition
            or begin.group(4) != self.rockos_boot_id
            or begin.group(5) != "0"
            or end.group(1) != self.measurement_nonce
            or end.group(2) != "3"
            or end.group(3) != "0"
        ):
            raise HostGateError("deployment measurement header is invalid")
        for match, artifact in zip(artifact_matches, self.artifacts):
            if (
                match.group(1) != self.measurement_nonce
                or match.group(2) != artifact.name
                or match.group(3) != artifact.mmc_path
                or int(match.group(4)) != artifact.size
                or match.group(5) != artifact.sha256
                or match.group(6) != "0"
            ):
                raise HostGateError(
                    f"{artifact.name}: deployment measurement record mismatch"
                )
        native_records: list[tuple[str, re.Match[str]]] = []
        for line in normalized.splitlines():
            if match := _MEASUREMENT_NATIVE_STAT.fullmatch(line):
                native_records.append(("stat", match))
            elif match := _MEASUREMENT_NATIVE_SHA256.fullmatch(line):
                native_records.append(("sha256", match))
        expected_native = tuple(
            record
            for artifact in self.artifacts
            for record in (
                ("stat", str(artifact.size), artifact.mmc_path),
                ("sha256", artifact.sha256, artifact.mmc_path),
            )
        )
        observed_native = tuple(
            (kind, match.group(1), match.group(2)) for kind, match in native_records
        )
        if observed_native != expected_native:
            raise HostGateError("deployment native measurement output mismatch")
        try:
            validate_recovery_epoch(normalized[normalized.index(end.group(0)) :])
        except (RuntimeError, ValueError) as error:
            raise HostGateError(
                "deployment measurement lacks a fresh recovery epoch"
            ) from error

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(asdict(self), separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()


def _parse_measurement_protocol(
    payload: bytes,
) -> tuple[str, list[re.Match[str]]]:
    if (
        not isinstance(payload, bytes)
        or not 0 < len(payload) <= MAX_DEPLOYMENT_MEASUREMENT_BYTES
    ):
        raise HostGateError("deployment measurement log is not bounded bytes")
    try:
        transcript = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HostGateError("deployment measurement log is not UTF-8") from error
    normalized = TERMINAL_ESCAPE_PATTERN.sub("", transcript).replace("\r", "")
    protocol: list[re.Match[str]] = []
    for line in normalized.splitlines():
        for pattern in (
            _MEASUREMENT_BEGIN,
            _MEASUREMENT_ARTIFACT,
            _MEASUREMENT_END,
        ):
            if match := pattern.fullmatch(line):
                protocol.append(match)
                break
    if len(protocol) != 5:
        raise HostGateError("deployment measurement frame is incomplete")
    begin, *artifact_matches, end = protocol
    if begin.re is not _MEASUREMENT_BEGIN or end.re is not _MEASUREMENT_END:
        raise HostGateError("deployment measurement frame is out of order")
    if any(match.re is not _MEASUREMENT_ARTIFACT for match in artifact_matches):
        raise HostGateError("deployment measurement artifacts are out of order")
    return normalized, protocol


@dataclass(frozen=True)
class BootStabilityResult:
    """Canonical result for one three-epoch physical boot attempt."""

    schema_version: int
    passed: bool
    physical: bool
    reason: str
    plan_sha256: str
    bootargs_sha256: str
    requested_cycles: int
    completed_cycles: int
    cycles: tuple[BootCycleEvidence, ...]
    attempts: tuple[BootAttemptEvidence, ...]

    def __post_init__(self) -> None:
        if (
            self.schema_version != 2
            or self.physical is not True
            or not isinstance(self.passed, bool)
            or not isinstance(self.reason, str)
            or not self.reason
            or _SHA256.fullmatch(self.plan_sha256) is None
            or _SHA256.fullmatch(self.bootargs_sha256) is None
            or self.requested_cycles != BOOT_STABILITY_CYCLES
            or self.completed_cycles != len(self.cycles)
            or len(self.attempts) < len(self.cycles)
            or len(self.attempts) > self.requested_cycles
            or tuple(attempt.cycle for attempt in self.attempts)
            != tuple(range(1, len(self.attempts) + 1))
            or any(
                not attempt.recovered or attempt.cycle != cycle.cycle
                for attempt, cycle in zip(self.attempts, self.cycles)
            )
        ):
            raise HostGateError("boot-stability result is invalid")
        if self.passed:
            if (
                self.reason != "boot-stability-pass"
                or self.completed_cycles != self.requested_cycles
                or len(self.attempts) != self.requested_cycles
            ):
                raise HostGateError("passing boot-stability result is incomplete")
        elif self.reason == "boot-stability-pass":
            raise HostGateError("failed boot-stability result uses pass reason")

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(asdict(self), separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()


@dataclass(frozen=True)
class BootCycleRecord:
    """Private retained output from a successful or failed boot cycle."""

    cycle: int
    serial: bytes
    diagnostics: bytes
    recovered: bool


class BootCycleOperations(Protocol):
    """Side effects required for one physical boot and recovery epoch."""

    @property
    def guest_started(self) -> bool: ...

    @property
    def transcript(self) -> str | bytes: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: Any, timeout: float) -> tuple[str, ...]: ...

    def boot(self, plan: Any, bootargs: str, timeout: float) -> None: ...

    def prove_boot_readiness(self, timeout: float) -> BootReadinessEvidence: ...

    def collect_diagnostics(self, timeout: float) -> bytes: ...

    def request_reboot(self, timeout: float) -> None: ...

    def await_recovery(self, timeout: float) -> None: ...

    def close(self) -> None: ...


class BootStabilityPublisher(Protocol):
    """Atomic output operations for one complete boot-stability attempt."""

    def invalidate(self) -> None: ...

    def publish(
        self, result: BootStabilityResult, records: tuple[BootCycleRecord, ...]
    ) -> None: ...


class _FatalDiagnosticsError(HostGateError):
    pass


def boot_stability_bootargs(plan: Any) -> str:
    """Removes network activation and fixture inputs from the physical boot."""

    excluded = (
        "asterinas.net=",
        "asterinas.neighbor=",
        "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_",
    )
    return " ".join(
        token
        for token in physical_bootargs(plan).split()
        if not token.startswith(excluded)
    )


def contains_fatal_diagnostics(payload: bytes) -> bool:
    """Reports whether a bounded diagnostic payload contains a fatal marker."""

    if not isinstance(payload, bytes):
        raise HostGateError("boot diagnostics must be bytes")
    lowered = payload.lower()
    return any(marker in lowered for marker in _FATAL_DIAGNOSTIC_MARKERS)


def boot_readiness_commands(nonce: str) -> tuple[str, ...]:
    """Returns short commands for the input-independent display probe."""

    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise HostGateError("readiness nonce is invalid")
    return (
        "_asterinas_boot_framebuffer=0; [ -c /dev/fb0 ] && "
        "_asterinas_boot_framebuffer=1; :",
        "_asterinas_boot_xorg=0; "
        "for _asterinas_boot_xorg_pid in $(pgrep -x Xorg 2>/dev/null); do "
        "for _asterinas_boot_xorg_fd in "
        "/proc/$_asterinas_boot_xorg_pid/fd/*; do "
        '[ "$(readlink "$_asterinas_boot_xorg_fd" 2>/dev/null)" = /dev/fb0 ] && '
        "[ -S /tmp/.X11-unix/X0 ] && _asterinas_boot_xorg=1; "
        "done; done; :",
        "_asterinas_boot_openbox=0; "
        "pgrep -u 1000 -x openbox >/dev/null 2>&1 && "
        "_asterinas_boot_openbox=1; :",
        "_asterinas_boot_service=$(systemctl is-active "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_boot_pid=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_boot_restarts=$(systemctl show --property NRestarts --value "
        "asterinas-browser-web.service 2>/dev/null || true); :",
        "_asterinas_boot_firefox=0; "
        "case $_asterinas_boot_pid in ''|*[!0-9]*) ;; *) "
        "grep -Eq '^firefox(-esr)?$' \"/proc/$_asterinas_boot_pid/comm\" "
        "2>/dev/null && _asterinas_boot_firefox=1 ;; esac; :",
        f"printf '__ASTERINAS_BOOT_PREFLIGHT__ nonce={nonce} "
        "browser_pid=%s framebuffer=%s "
        "xorg_fbdev=%s openbox=%s firefox=%s browser_service=%s "
        "browser_restarts=%s\\n' "
        '"$_asterinas_boot_pid" "$_asterinas_boot_framebuffer" '
        '"$_asterinas_boot_xorg" "$_asterinas_boot_openbox" '
        '"$_asterinas_boot_firefox" "$_asterinas_boot_service" '
        '"$_asterinas_boot_restarts"',
    )


def parse_boot_readiness_marker(
    line: str, expected_nonce: str
) -> BootReadinessEvidence:
    """Parses one exact input-independent boot readiness marker."""

    match = _BOOT_PREFLIGHT.fullmatch(line)
    if match is None:
        raise HostGateError("boot readiness marker is malformed")
    if match.group(1) != expected_nonce:
        raise HostGateError("boot readiness nonce mismatch")
    try:
        return BootReadinessEvidence(
            browser_pid=int(match.group(2)),
            framebuffer=match.group(3) == "1",
            xorg_fbdev=match.group(4) == "1",
            openbox=match.group(5) == "1",
            firefox=match.group(6) == "1",
            browser_service=match.group(7),
            browser_restarts=int(match.group(8)),
        )
    except HostGateError as error:
        snapshot = line.removeprefix("__ASTERINAS_BOOT_PREFLIGHT__ ")
        raise HostGateError(f"boot readiness incomplete: {snapshot}") from error


def boot_diagnostics_commands(nonce: str) -> tuple[str, ...]:
    """Returns short commands that emit one authenticated diagnostic frame."""

    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise HostGateError("diagnostic nonce is invalid")
    diagnostic_path = f"/run/asterinas-boot-diag-{nonce}"
    return (
        f"_asterinas_boot_diag={diagnostic_path}; "
        'rm -f -- "$_asterinas_boot_diag" "$_asterinas_boot_diag".*; '
        ': >"$_asterinas_boot_diag"',
        "{ printf '%s\\n' '== cmdline =='; cat /proc/cmdline; } "
        '>"$_asterinas_boot_diag.01" 2>&1',
        "{ printf '%s\\n' '== uptime =='; cat /proc/uptime; "
        "printf '%s\\n' '== mounts =='; cat /proc/mounts; } "
        '>"$_asterinas_boot_diag.02" 2>&1',
        "{ printf '%s\\n' '== dmesg =='; dmesg --color=never; } "
        '>"$_asterinas_boot_diag.03" 2>&1',
        "{ printf '%s\\n' '== failed units =='; "
        "systemctl --failed --no-legend --no-pager || true; } "
        '>"$_asterinas_boot_diag.04" 2>&1',
        "{ printf '%s\\n' '== graphical units =='; "
        "systemctl show --no-pager "
        "--property Id,ActiveState,SubState,MainPID,NRestarts "
        "asterinas-desktop-m4.service asterinas-desktop-m5.service "
        "asterinas-browser-web.service || true; } "
        '>"$_asterinas_boot_diag.05" 2>&1',
        "{ printf '%s\\n' '== process tree =='; "
        "ps -eo pid,ppid,stat,wchan:24,comm,args || true; } "
        '>"$_asterinas_boot_diag.06" 2>&1',
        "{ printf '%s\\n' '== graphical service status =='; "
        "systemctl status --no-pager --full asterinas-desktop-m5.service "
        "asterinas-browser-web.service || true; } "
        '>"$_asterinas_boot_diag.07" 2>&1',
        "{ printf '%s\\n' '== graphical journal =='; "
        "journalctl -b --no-pager -n 200 -u asterinas-desktop-m5.service "
        "-u asterinas-browser-web.service || true; } "
        '>"$_asterinas_boot_diag.08" 2>&1',
        "{ printf '%s\\n' '== graphical logs =='; "
        "for _asterinas_boot_log in /home/asterinas/Xorg.0.log "
        "/home/asterinas/desktop-m5-session.log "
        "/home/asterinas/firefox-web-stderr.log "
        "/home/asterinas/firefox-web-mozilla.log; do "
        "printf '%s\\n' \"--- $_asterinas_boot_log ---\"; "
        'tail -n 200 "$_asterinas_boot_log" 2>&1 || true; done; } '
        '>"$_asterinas_boot_diag.09" 2>&1',
        ': >"$_asterinas_boot_diag"; '
        'for _asterinas_boot_part in "$_asterinas_boot_diag".*; do '
        '[ -f "$_asterinas_boot_part" ] && '
        'cat "$_asterinas_boot_part" >>"$_asterinas_boot_diag"; done; '
        '_asterinas_boot_diag_size=$(wc -c <"$_asterinas_boot_diag"); '
        f'if [ "$_asterinas_boot_diag_size" -le {MAX_DIAGNOSTICS_BYTES} ]; then '
        '_asterinas_boot_diag_sha=$(sha256sum "$_asterinas_boot_diag" | '
        "cut -d' ' -f1); else _asterinas_boot_diag_sha=" + "0" * 64 + "; fi",
        f'if [ "$_asterinas_boot_diag_size" -le {MAX_DIAGNOSTICS_BYTES} ]; then '
        "printf '__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce="
        f"{nonce} size=%s sha256=%s\\n' "
        '"$_asterinas_boot_diag_size" "$_asterinas_boot_diag_sha"; '
        "base64 -w 0 \"$_asterinas_boot_diag\"; printf '\\n'; "
        "printf '__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce="
        f"{nonce} status=0\\n'; else "
        "printf '__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce="
        f"{nonce} size=%s sha256=%s\\n' "
        '"$_asterinas_boot_diag_size" "$_asterinas_boot_diag_sha"; '
        "printf '__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce="
        f'{nonce} status=1\\n\'; fi; rm -f -- "$_asterinas_boot_diag" '
        '"$_asterinas_boot_diag".*',
    )


class RealBootCycleOperations(RealPhysicalGraphicsOperations):
    """Physical graphics adapter extended with unattended boot operations."""

    def prove_boot_readiness(self, timeout: float) -> BootReadinessEvidence:
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        serial.wait_for(DEBUG_CONSOLE_READY.encode(), deadline)
        try:
            transcript = serial.transcript.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HostGateError("debug console transcript is not UTF-8") from error
        validate_debug_console_readiness(transcript)
        self._quiesce_external_services(deadline)
        run_debug_console_phase(
            serial,
            deadline,
            secrets.token_hex(16),
            ready_seen=True,
        )

        last_error: HostGateError | None = None
        while True:
            try:
                nonce = secrets.token_hex(8)
                commands = boot_readiness_commands(nonce)
                for step, command in enumerate(commands[:-1], start=1):
                    self._send_guest_step(command, f"readiness-{step}", nonce, deadline)
                cursor = serial.checkpoint()
                probe_deadline = min(deadline, time.monotonic() + GUEST_STEP_TIMEOUT)
                self._send_bounded(serial, commands[-1], probe_deadline)
                while True:
                    line, cursor = self._next_line(serial, cursor, probe_deadline)
                    if not line.startswith("__ASTERINAS_BOOT_PREFLIGHT__"):
                        continue
                    try:
                        evidence = parse_boot_readiness_marker(line, nonce)
                    except HostGateError as error:
                        last_error = error
                    else:
                        self._browser_pid = evidence.browser_pid
                        self._sync_serial_log()
                        return evidence
                    break
            except TimeoutError as error:
                self._abort_guest_shell()
                if last_error is not None:
                    raise last_error from error
                if deadline - time.monotonic() > GUEST_STEP_TIMEOUT:
                    continue
                raise HostGateError("boot readiness timed out") from error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last_error or HostGateError("boot readiness timed out")
            time.sleep(min(1.0, remaining))

    def collect_diagnostics(self, timeout: float) -> bytes:
        nonce = secrets.token_hex(8)
        commands = boot_diagnostics_commands(nonce)
        return self._collect_diagnostics_commands(timeout, nonce, commands)

    def _collect_diagnostics_commands(
        self,
        timeout: float,
        nonce: str,
        commands: Sequence[str],
    ) -> bytes:
        """Run a bounded diagnostic command set and verify its serial frame."""

        if re.fullmatch(r"[0-9a-f]{16}", nonce) is None or not commands:
            raise HostGateError("diagnostic command contract is invalid")
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        for step, command in enumerate(commands[:-1], start=1):
            self._send_guest_step(command, f"diagnostics-{step}", nonce, deadline)
        cursor = serial.checkpoint()
        self._send_bounded(serial, commands[-1], deadline)

        while True:
            line, cursor = self._next_line(serial, cursor, deadline)
            begin = _DIAGNOSTICS_BEGIN.fullmatch(line)
            if begin is None or begin.group(1) != nonce:
                continue
            expected_size = int(begin.group(2))
            expected_sha256 = begin.group(3)
            if expected_size > MAX_DIAGNOSTICS_BYTES:
                raise HostGateError("boot diagnostics exceed the bounded size")
            break

        encoded, cursor = self._next_line(serial, cursor, deadline)
        end, cursor = self._next_line(serial, cursor, deadline)
        expected_end = f"__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce={nonce} status=0"
        if end != expected_end:
            raise HostGateError("boot diagnostics end marker is invalid")
        expected_encoded_size = ((expected_size + 2) // 3) * 4
        if len(encoded) != expected_encoded_size:
            raise HostGateError("boot diagnostics encoded length is invalid")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HostGateError("boot diagnostics are not canonical base64") from error
        if (
            len(payload) != expected_size
            or hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise HostGateError("boot diagnostics identity mismatch")
        self._sync_serial_log()
        return payload

    def _send_guest_step(
        self,
        command: str,
        step: str,
        nonce: str,
        deadline: float,
    ) -> None:
        serial = self._require_serial()
        marker_prefix = f"__ASTERINAS_BOOT_STEP__ nonce={nonce} step={step} status="
        success_marker = marker_prefix + "0"
        while True:
            now = time.monotonic()
            full_step_window = now + GUEST_STEP_TIMEOUT <= deadline
            step_deadline = min(deadline, now + GUEST_STEP_TIMEOUT)
            cursor = serial.checkpoint()
            try:
                self._send_bounded(
                    serial,
                    f"{command}; _asterinas_boot_status=$?; "
                    'if [ "$_asterinas_boot_status" -eq 0 ]; then '
                    f"printf '{success_marker}\\n'; else "
                    f"printf '{marker_prefix}%s\\n' "
                    '"$_asterinas_boot_status"; fi',
                    step_deadline,
                )
                while True:
                    line, cursor = self._next_line(serial, cursor, step_deadline)
                    if not line.startswith(marker_prefix):
                        continue
                    status = line.removeprefix(marker_prefix)
                    if re.fullmatch(r"[0-9]+", status) is None:
                        raise HostGateError(f"{step} status is malformed")
                    if status != "0":
                        raise HostGateError(f"{step} failed with status {status}")
                    return
            except TimeoutError:
                self._abort_guest_shell()
                if not full_step_window:
                    raise

    def _abort_guest_shell(self) -> None:
        """Restore an interactive shell command boundary after a lost ACK."""

        serial = self._require_serial()
        serial.send(b"\x03\n", time.monotonic() + GUEST_ABORT_TIMEOUT)

    @staticmethod
    def _send_bounded(serial: Any, command: str, deadline: float) -> None:
        payload = (command + "\n").encode()
        if len(payload) > MAX_SERIAL_COMMAND_BYTES:
            raise HostGateError("serial shell command exceeds the safe size")
        serial.send(payload, deadline)

    def request_reboot(self, timeout: float) -> None:
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        nonce = secrets.token_hex(8)
        marker = f"__ASTERINAS_BOOT_REBOOT__ nonce={nonce}"
        cursor = serial.checkpoint()
        self._recovery_cursor = cursor
        self._send_bounded(serial, f"sync; printf '{marker}\\n'; reboot -f", deadline)
        while True:
            line, cursor = self._next_line(serial, cursor, deadline)
            if line == marker:
                self._sync_serial_log()
                return

    def await_recovery(self, timeout: float) -> None:
        serial = self._require_serial()
        cursor = getattr(self, "_recovery_cursor", 0)
        deadline = time.monotonic() + timeout
        serial.wait_for(b"U-Boot ", deadline, start=cursor)
        serial.send(b"\n", deadline)
        serial.wait_for(b"=> ", deadline, start=cursor)
        recovery = serial.transcript[cursor:].decode("utf-8", errors="replace")
        validate_recovery_epoch(recovery)
        self._sync_serial_log()


class RealBootStabilityPublisher:
    """Atomically publishes deployment identity and per-cycle boot evidence."""

    _OUTPUT_NAMES = (
        "result.json",
        "sha256sums.txt",
        "deployment.json",
        "deployment-attestation.json",
        "deployment-measurement.serial.log",
        "cycle-1.serial.log",
        "cycle-1.diagnostics.log",
        "cycle-2.serial.log",
        "cycle-2.diagnostics.log",
        "cycle-3.serial.log",
        "cycle-3.diagnostics.log",
    )

    def __init__(
        self,
        plan: Any,
        output_directory: Path,
        mmc_artifacts: Mapping[str, str],
        deployment_attestation: DeploymentAttestation,
        deployment_measurement_log: bytes,
        *,
        repository: Path | None = None,
    ) -> None:
        expected = {"kernel", "initramfs", "megrez_dtb"}
        if set(mmc_artifacts) != expected:
            raise HostGateError("boot stability requires three MMC artifacts")
        self._plan = plan
        self._output_directory = output_directory
        self._mmc_artifacts = dict(mmc_artifacts)
        deployment_attestation.validate(
            plan, self._mmc_artifacts, deployment_measurement_log
        )
        self._deployment_attestation = deployment_attestation
        self._deployment_measurement_log = deployment_measurement_log
        self._repository = (
            repository.absolute()
            if repository is not None
            else Path(__file__).resolve().parents[2]
        )
        self._output: PinnedOutputDirectory | None = None

    def invalidate(self) -> None:
        if self._output is None:
            output_path = _safe_output_directory(
                self._output_directory, self._repository
            )
            output = PinnedOutputDirectory(output_path)
            try:
                output.lock_exclusive()
            except RuntimeError as error:
                output.close()
                raise HostGateError(
                    "boot-stability output run is already active"
                ) from error
            self._output = output
        self._output.invalidate(*self._OUTPUT_NAMES)

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def publish(
        self, result: BootStabilityResult, records: tuple[BootCycleRecord, ...]
    ) -> None:
        if self._output is None:
            raise HostGateError("boot-stability output is not pinned")
        output, self._output = self._output, None
        try:
            if (
                len(records) > BOOT_STABILITY_CYCLES
                or tuple(record.cycle for record in records)
                != tuple(range(1, len(records) + 1))
                or any(
                    not isinstance(record.serial, bytes)
                    or not isinstance(record.diagnostics, bytes)
                    or len(record.diagnostics) > MAX_DIAGNOSTICS_BYTES
                    for record in records
                )
            ):
                raise HostGateError("boot-stability records are invalid")

            output_names: list[str] = []
            for record in records:
                serial_name = f"cycle-{record.cycle}.serial.log"
                diagnostics_name = f"cycle-{record.cycle}.diagnostics.log"
                output.atomic_write(serial_name, record.serial, mode=0o600)
                output.atomic_write(diagnostics_name, record.diagnostics, mode=0o600)
                output_names.extend((serial_name, diagnostics_name))

            identities = {identity.name: identity for identity in self._plan.artifacts}
            attested = {
                artifact.name: artifact
                for artifact in self._deployment_attestation.artifacts
            }
            attestation_payload = self._deployment_attestation.canonical_bytes()
            output.atomic_write(
                "deployment-attestation.json", attestation_payload, mode=0o600
            )
            output_names.append("deployment-attestation.json")
            output.atomic_write(
                "deployment-measurement.serial.log",
                self._deployment_measurement_log,
                mode=0o600,
            )
            output_names.append("deployment-measurement.serial.log")
            deployment = {
                "schema_version": 3,
                "plan_sha256": self._plan.plan_sha256,
                "attestation_sha256": hashlib.sha256(attestation_payload).hexdigest(),
                "measurement_log_sha256": hashlib.sha256(
                    self._deployment_measurement_log
                ).hexdigest(),
                "bootargs_sha256": hashlib.sha256(
                    boot_stability_bootargs(self._plan).encode()
                ).hexdigest(),
                "artifacts": {
                    name: {
                        "mmc_path": self._mmc_artifacts[name],
                        "size": attested[name].size,
                        "sha256": attested[name].sha256,
                        "crc32": identities[name].crc32,
                    }
                    for name in ("kernel", "initramfs", "megrez_dtb")
                },
            }
            deployment_payload = (
                json.dumps(deployment, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode()
            output.atomic_write("deployment.json", deployment_payload, mode=0o600)
            output_names.append("deployment.json")

            result_payload = result.canonical_bytes()
            sums = [f"{output.sha256(name)}  {name}" for name in output_names]
            sums.append(f"{hashlib.sha256(result_payload).hexdigest()}  result.json")
            output.atomic_write(
                "sha256sums.txt", ("\n".join(sums) + "\n").encode(), mode=0o600
            )
            output.atomic_write("result.json", result_payload, mode=0o600)
        finally:
            output.close()


def _read_bounded_regular(path: Path, label: str, maximum: int) -> bytes:
    """Read one bounded regular file through a no-follow descriptor."""

    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HostGateError(f"cannot open {label}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise HostGateError(f"{label} is not a bounded regular file")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size:
            raise HostGateError(f"{label} changed while being read")
    finally:
        os.close(descriptor)
    return bytes(payload)


def _read_deployment_attestation(path: Path) -> DeploymentAttestation:
    """Read one small no-follow RockOS deployment receipt."""

    payload = _read_bounded_regular(path, "deployment attestation", 64 * 1024)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostGateError("deployment attestation is not valid JSON") from error
    return DeploymentAttestation.from_mapping(value)


def _invalidate_boot_stability_output(output_directory: Path) -> None:
    """Remove a stale terminal result before reading any new deployment input."""

    repository = Path(__file__).resolve().parents[2]
    output_path = _safe_output_directory(output_directory, repository)
    with PinnedOutputDirectory(output_path) as output:
        try:
            output.lock_exclusive()
        except RuntimeError as error:
            raise HostGateError(
                "boot-stability output run is already active"
            ) from error
        output.invalidate(*RealBootStabilityPublisher._OUTPUT_NAMES)


def parse_args(arguments: Sequence[str]) -> argparse.Namespace:
    """Parses the MMC-only unattended physical boot command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--deployment-attestation", required=True, type=Path)
    parser.add_argument("--deployment-measurement-log", required=True, type=Path)
    parser.add_argument("--mmc-kernel", required=True, type=safe_artifact_name)
    parser.add_argument("--mmc-initramfs", required=True, type=safe_artifact_name)
    parser.add_argument("--mmc-dtb", required=True, type=safe_artifact_name)
    parser.add_argument("--open-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--artifact-timeout", type=_positive_seconds, default=300.0)
    parser.add_argument("--boot-timeout", type=_positive_seconds, default=180.0)
    parser.add_argument("--readiness-timeout", type=_positive_seconds, default=240.0)
    parser.add_argument("--diagnostics-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--reboot-timeout", type=_positive_seconds, default=30.0)
    parser.add_argument("--recovery-timeout", type=_positive_seconds, default=180.0)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Runs the physical gate and prints its canonical terminal result."""

    values = parse_args(sys.argv[1:] if arguments is None else arguments)
    publisher: RealBootStabilityPublisher | None = None
    try:
        _invalidate_boot_stability_output(values.output_directory)
        plan = _read_plan(values.plan)
        deployment_attestation = _read_deployment_attestation(
            values.deployment_attestation
        )
        deployment_measurement_log = _read_bounded_regular(
            values.deployment_measurement_log,
            "deployment measurement log",
            MAX_DEPLOYMENT_MEASUREMENT_BYTES,
        )
        config = BootStabilityConfig(
            open_timeout=values.open_timeout,
            artifact_timeout=values.artifact_timeout,
            boot_timeout=values.boot_timeout,
            readiness_timeout=values.readiness_timeout,
            diagnostics_timeout=values.diagnostics_timeout,
            reboot_timeout=values.reboot_timeout,
            recovery_timeout=values.recovery_timeout,
        )
        mmc_artifacts = {
            "kernel": values.mmc_kernel,
            "initramfs": values.mmc_initramfs,
            "megrez_dtb": values.mmc_dtb,
        }
        output_directory = values.output_directory
        publisher = RealBootStabilityPublisher(
            plan,
            output_directory,
            mmc_artifacts,
            deployment_attestation,
            deployment_measurement_log,
        )

        def operations_factory(_cycle: int) -> RealBootCycleOperations:
            return RealBootCycleOperations(
                plan,
                values.device,
                output_directory,
                output_directory / ".unused-hdmi-capture",
                mmc_artifacts=mmc_artifacts,
            )

        result = run_boot_stability(plan, config, operations_factory, publisher)
    except (HostGateError, OSError, RuntimeError, ValueError) as error:
        print(
            f"boot stability gate failed before publication: {error}",
            file=sys.stderr,
        )
        return 2
    finally:
        if publisher is not None:
            publisher.close()
    print(result.canonical_bytes().decode(), end="")
    return 0 if result.passed else 1


def run_boot_stability(
    plan: Any,
    config: BootStabilityConfig,
    operations_factory: Callable[[int], BootCycleOperations],
    publisher: BootStabilityPublisher,
    *,
    artifact_validator: Callable[[Any], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> BootStabilityResult:
    """Runs three fresh physical boots and publishes after terminal recovery."""

    plan.validate()
    if artifact_validator is not None:
        artifact_validator(plan)
    bootargs = boot_stability_bootargs(plan)
    publisher.invalidate()
    evidence: list[BootCycleEvidence] = []
    attempts: list[BootAttemptEvidence] = []
    records: list[BootCycleRecord] = []
    reason = "boot-stability-incomplete"

    for cycle in range(1, BOOT_STABILITY_CYCLES + 1):
        operations = operations_factory(cycle)
        transport: tuple[str, ...] = ()
        readiness: BootReadinessEvidence | None = None
        diagnostics = b""
        recovered = False
        failure: BaseException | None = None
        recovery_failure: BaseException | None = None
        interruption: BaseException | None = None
        artifact_seconds = 0.0
        readiness_seconds = 0.0
        diagnostics_seconds = 0.0
        recovery_seconds = 0.0
        guest_deadline: float | None = None

        try:
            artifact_start = clock()
            operations.open(config.open_timeout)
            transport = operations.ensure_artifacts(plan, config.artifact_timeout)
            artifact_seconds = _elapsed_seconds(clock, artifact_start)

            readiness_start = clock()
            operations.boot(plan, bootargs, config.boot_timeout)
            guest_deadline = clock() + PHYSICAL_REBOOT_AFTER
            readiness = operations.prove_boot_readiness(config.readiness_timeout)
            readiness_seconds = _elapsed_seconds(clock, readiness_start)

            diagnostics_start = clock()
            diagnostics = operations.collect_diagnostics(config.diagnostics_timeout)
            diagnostics_seconds = _elapsed_seconds(clock, diagnostics_start)
            if contains_fatal_diagnostics(diagnostics):
                raise _FatalDiagnosticsError("fatal diagnostics")
        except Exception as error:
            failure = error
        except BaseException as error:
            interruption = error

        if operations.guest_started:
            if not diagnostics:
                diagnostics_start = clock()
                try:
                    diagnostics = operations.collect_diagnostics(
                        config.diagnostics_timeout
                    )
                except Exception as diagnostic_error:
                    if failure is None:
                        failure = diagnostic_error
                diagnostics_seconds = _elapsed_seconds(clock, diagnostics_start)

            recovery_start = clock()
            try:
                operations.request_reboot(config.reboot_timeout)
            except Exception as reboot_error:
                recovery_failure = reboot_error
            try:
                remaining_guest = (
                    max(0.0, guest_deadline - clock())
                    if guest_deadline is not None
                    else 0.0
                )
                operations.await_recovery(config.recovery_timeout + remaining_guest)
                recovered = True
            except Exception as recovery_error:
                if recovery_failure is None:
                    recovery_failure = recovery_error
            recovery_seconds = _elapsed_seconds(clock, recovery_start)

        try:
            serial = _transcript_bytes(operations.transcript)
        finally:
            operations.close()
        records.append(
            BootCycleRecord(
                cycle=cycle,
                serial=serial,
                diagnostics=diagnostics,
                recovered=recovered,
            )
        )
        attempts.append(
            BootAttemptEvidence(
                cycle=cycle,
                serial_sha256=hashlib.sha256(serial).hexdigest(),
                diagnostics_sha256=hashlib.sha256(diagnostics).hexdigest(),
                recovered=recovered,
            )
        )

        if interruption is not None:
            raise interruption
        if failure is None and contains_fatal_diagnostics(serial):
            failure = _FatalDiagnosticsError("fatal serial transcript")
        if failure is None and recovery_failure is not None:
            failure = recovery_failure
        if failure is not None:
            if isinstance(failure, _FatalDiagnosticsError):
                reason = f"cycle-{cycle}-fatal-diagnostics"
            else:
                reason = f"cycle-{cycle}-{_failure_reason(failure)}"
            if recovery_failure is not None and recovery_failure is not failure:
                reason += f"-and-recovery-{_failure_reason(recovery_failure)}"
            break
        if not recovered or readiness is None:
            reason = f"cycle-{cycle}-incomplete"
            break

        evidence.append(
            BootCycleEvidence(
                cycle=cycle,
                readiness=readiness,
                transport=transport,
                serial_sha256=hashlib.sha256(serial).hexdigest(),
                diagnostics_sha256=hashlib.sha256(diagnostics).hexdigest(),
                artifact_seconds=artifact_seconds,
                readiness_seconds=readiness_seconds,
                diagnostics_seconds=diagnostics_seconds,
                recovery_seconds=recovery_seconds,
                recovered=True,
            )
        )

    passed = len(evidence) == BOOT_STABILITY_CYCLES
    if passed:
        reason = "boot-stability-pass"
    result = BootStabilityResult(
        schema_version=2,
        passed=passed,
        physical=True,
        reason=reason,
        plan_sha256=plan.plan_sha256,
        bootargs_sha256=hashlib.sha256(bootargs.encode()).hexdigest(),
        requested_cycles=BOOT_STABILITY_CYCLES,
        completed_cycles=len(evidence),
        cycles=tuple(evidence),
        attempts=tuple(attempts),
    )
    publisher.publish(result, tuple(records))
    return result


def _elapsed_seconds(clock: Callable[[], float], start: float) -> float:
    elapsed = clock() - start
    if not math.isfinite(elapsed) or elapsed < 0:
        raise HostGateError("monotonic clock moved backwards")
    return round(elapsed, 3)


def _transcript_bytes(transcript: str | bytes) -> bytes:
    if isinstance(transcript, str):
        return transcript.encode("utf-8")
    if isinstance(transcript, bytes):
        return transcript
    raise HostGateError("serial transcript must be text or bytes")


def _failure_reason(error: BaseException) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", type(error).__name__).lower()
    detail = re.sub(r"[^a-z0-9]+", "-", str(error).lower()).strip("-")[:96]
    return f"{name}-{detail or 'unspecified'}"


if __name__ == "__main__":
    raise SystemExit(main())
