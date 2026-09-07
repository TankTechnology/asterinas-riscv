#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Fail-closed serial protocol for the opt-in Asterinas root console."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence


MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES = 8 * 1024 * 1024
DEBUG_CONSOLE_READY = "ASTERINAS_DEBUG_CONSOLE_READY uid=0"

_NONCE_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_ROOT_DEVICE_RE = re.compile(r"\A(?:/dev/vd[a-z]|/dev/mmcblk0p2)\Z")
_PROTOCOL_NONCE_RE = re.compile(r"__ASTERINAS_DEBUG_([0-9a-f]{32})_")


class DebugConsoleProtocolError(ValueError):
    """The serial exchange does not prove the required debug-console state."""


@dataclass(frozen=True)
class DebugConsoleCommand:
    """One fixed, read-only probe with nonce-bound line markers."""

    name: str
    payload: str
    begin_marker: str
    status_prefix: str
    end_marker: str


@dataclass(frozen=True)
class DebugConsoleEvidence:
    """Identity and graphical-state evidence collected through the root shell."""

    uid: int
    pid1: str
    root_device: str
    root_filesystem: str
    graphical_state: str
    desktop_state: str


class DebugConsoleSerial(Protocol):
    @property
    def transcript(self) -> bytes: ...

    def checkpoint(self) -> int: ...

    def send(self, payload: bytes, deadline: float) -> None: ...

    def wait_for(
        self, marker: bytes, deadline: float, *, start: int = 0
    ) -> bytes: ...

    def wait_for_any(
        self, markers: Sequence[bytes], deadline: float, *, start: int = 0
    ) -> bytes: ...


def _validate_nonce(nonce: str) -> None:
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
        raise ValueError("debug-console nonce must be exactly 32 lowercase hex digits")


def debug_console_commands(nonce: str) -> tuple[DebugConsoleCommand, ...]:
    """Return the complete fixed probe set bound to ``nonce``."""

    _validate_nonce(nonce)
    probes = (
        ("uid", "UID", "id -u"),
        ("pid1", "PID1", "tr -d '\\n' </proc/1/comm; printf '\\n'"),
        ("root", "ROOT", "awk '$2 == \"/\" { print $1, $3; exit }' /proc/mounts"),
        ("graphical", "GRAPHICAL", "systemctl is-active graphical.target"),
        (
            "desktop",
            "DESKTOP",
            "systemctl is-active asterinas-desktop-m5.service",
        ),
    )
    commands = []
    for name, token, probe in probes:
        prefix = f"__ASTERINAS_DEBUG_{nonce}_{token}"
        begin = f"{prefix}_BEGIN__"
        status = f"{prefix}_STATUS__"
        end = f"{prefix}_END__"
        payload = (
            f"printf '{begin}\\n'; {probe}; "
            "_asterinas_debug_status=$?; "
            f"printf '{status}%s\\n' \"$_asterinas_debug_status\"; "
            f"printf '{end}\\n'"
        )
        commands.append(
            DebugConsoleCommand(
                name=name,
                payload=payload,
                begin_marker=begin,
                status_prefix=status,
                end_marker=end,
            )
        )
    return tuple(commands)


def _lines(transcript: str | bytes) -> tuple[str, ...]:
    if isinstance(transcript, bytes):
        raw = transcript
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DebugConsoleProtocolError("serial transcript is not UTF-8") from error
    elif isinstance(transcript, str):
        text = transcript
        raw = text.encode("utf-8")
    else:
        raise DebugConsoleProtocolError("serial transcript must be bytes or text")
    if len(raw) > MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES:
        raise DebugConsoleProtocolError("serial transcript exceeds 8 MiB")
    if any(
        ord(character) < 0x20 and character not in "\r\n\t"
        or ord(character) == 0x7F
        for character in text
    ):
        raise DebugConsoleProtocolError("serial transcript contains control input")
    return tuple(line.rstrip("\r") for line in text.splitlines())


def _extract_outputs(
    lines: tuple[str, ...], commands: tuple[DebugConsoleCommand, ...], nonce: str
) -> dict[str, str]:
    observed_nonces = set(_PROTOCOL_NONCE_RE.findall("\n".join(lines)))
    if observed_nonces - {nonce}:
        raise DebugConsoleProtocolError("serial transcript contains stale nonce markers")

    expected_protocol_lines = {
        command.begin_marker for command in commands
    } | {command.end_marker for command in commands} | {
        f"{command.status_prefix}0" for command in commands
    }
    outputs: dict[str, str] = {}
    previous_end = -1
    for command in commands:
        begins = [
            index for index, line in enumerate(lines) if line == command.begin_marker
        ]
        statuses = [
            index
            for index, line in enumerate(lines)
            if line.startswith(command.status_prefix)
        ]
        ends = [
            index for index, line in enumerate(lines) if line == command.end_marker
        ]
        if len(begins) != 1 or len(statuses) != 1 or len(ends) != 1:
            raise DebugConsoleProtocolError(
                f"{command.name} command markers are missing or duplicated"
            )
        begin, status, end = begins[0], statuses[0], ends[0]
        if not previous_end < begin < status < end:
            raise DebugConsoleProtocolError("debug-console command markers are reordered")
        if lines[status] != f"{command.status_prefix}0":
            raise DebugConsoleProtocolError(
                f"{command.name} command returned a nonzero or invalid status"
            )
        output_lines = lines[begin + 1 : status]
        if len(output_lines) != 1:
            raise DebugConsoleProtocolError(
                f"{command.name} command output was not exactly one line"
            )
        outputs[command.name] = output_lines[0]
        previous_end = end

    for line in lines:
        if line.startswith(f"__ASTERINAS_DEBUG_{nonce}_") and (
            line not in expected_protocol_lines
        ):
            raise DebugConsoleProtocolError("serial transcript has an unknown marker")
    return outputs


def classify_debug_console(
    transcript: str | bytes, nonce: str
) -> DebugConsoleEvidence:
    """Validate a complete five-command exchange and return exact evidence."""

    commands = debug_console_commands(nonce)
    outputs = _extract_outputs(_lines(transcript), commands, nonce)
    if outputs["uid"] != "0":
        raise DebugConsoleProtocolError("debug console is not running as UID 0")
    if outputs["pid1"] != "systemd":
        raise DebugConsoleProtocolError("PID 1 is not systemd")
    root_fields = outputs["root"].split()
    if len(root_fields) != 2 or _ROOT_DEVICE_RE.fullmatch(root_fields[0]) is None:
        raise DebugConsoleProtocolError("root device identity is invalid")
    if root_fields[1] != "ext2":
        raise DebugConsoleProtocolError("root filesystem is not ext2")
    if outputs["graphical"] != "active":
        raise DebugConsoleProtocolError("graphical target is not active")
    if outputs["desktop"] != "active":
        raise DebugConsoleProtocolError("desktop service is not active")
    return DebugConsoleEvidence(
        uid=0,
        pid1="systemd",
        root_device=root_fields[0],
        root_filesystem=root_fields[1],
        graphical_state=outputs["graphical"],
        desktop_state=outputs["desktop"],
    )


def run_debug_console_phase(
    serial: DebugConsoleSerial,
    deadline: float,
    nonce: str,
    *,
    ready_seen: bool = False,
) -> DebugConsoleEvidence:
    """Run the fixed probes on an already booting serial console."""

    commands = debug_console_commands(nonce)
    if not ready_seen:
        serial.wait_for(DEBUG_CONSOLE_READY.encode(), deadline)
        ready_lines = tuple(
            line.rstrip("\r")
            for line in serial.transcript.decode("utf-8", errors="strict").splitlines()
        )
        if ready_lines.count(DEBUG_CONSOLE_READY) != 1:
            raise DebugConsoleProtocolError(
                "root-console readiness marker is missing or duplicated"
            )

    phase_start = serial.checkpoint()
    for command in commands:
        command_start = serial.checkpoint()
        serial.send(f"{command.payload}\n".encode(), deadline)
        serial.wait_for_any(
            (
                f"{command.end_marker}\r\n".encode(),
                f"{command.end_marker}\n".encode(),
            ),
            deadline,
            start=command_start,
        )
    return classify_debug_console(serial.transcript[phase_start:], nonce)
