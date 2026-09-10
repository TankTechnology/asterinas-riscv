#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Fail-closed serial protocol for the opt-in Asterinas root console."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Protocol, Sequence


MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES = 8 * 1024 * 1024
DEBUG_CONSOLE_READY = "ASTERINAS_DEBUG_CONSOLE_READY uid=0"
DEBUG_CONSOLE_ATTEMPTS = 2
DEBUG_CONSOLE_COMMAND_TIMEOUT = 15.0
DEBUG_CONSOLE_GRAPHICAL_TIMEOUT = 60.0
DEBUG_CONSOLE_ABORT_TIMEOUT = 3.0

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
    value_prefix: str
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

    def wait_for(self, marker: bytes, deadline: float, *, start: int = 0) -> bytes: ...

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
        ("pid1", "PID1", "tr -d '\\n' </proc/1/comm"),
        ("root", "ROOT", "awk '$2 == \"/\" { print $1, $3; exit }' /proc/mounts"),
        (
            "graphical",
            "GRAPHICAL",
            "_asterinas_debug_attempt=0; "
            "while ! systemctl is-active --quiet "
            "asterinas-desktop-m4-evidence.service && "
            "! systemctl is-active --quiet asterinas-desktop-m5.service; do "
            "_asterinas_debug_attempt=$((_asterinas_debug_attempt + 1)); "
            '[ "$_asterinas_debug_attempt" -ge 45 ] && break; sleep 1; done; '
            "if systemctl is-active --quiet asterinas-desktop-m4-evidence.service "
            "|| systemctl is-active --quiet asterinas-desktop-m5.service; then "
            "echo active; else echo inactive; false; fi",
        ),
        (
            "desktop",
            "DESKTOP",
            "if systemctl is-active --quiet asterinas-desktop-m4.service || "
            "systemctl is-active --quiet asterinas-desktop-m5.service; then "
            "echo active; else echo inactive; false; fi",
        ),
    )
    commands = []
    for name, token, probe in probes:
        prefix = f"__ASTERINAS_DEBUG_{nonce}_{token}"
        begin = f"{prefix}_BEGIN__"
        value = f"{prefix}_VALUE__"
        status = f"{prefix}_STATUS__"
        end = f"{prefix}_END__"
        payload = (
            f"_asterinas_debug_output=$({probe}); "
            "_asterinas_debug_status=$?; "
            f"printf '{begin}\\n{value}%s\\n{status}%s\\n{end}\\n' "
            '"$_asterinas_debug_output" "$_asterinas_debug_status"'
        )
        commands.append(
            DebugConsoleCommand(
                name=name,
                payload=payload,
                begin_marker=begin,
                value_prefix=value,
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
    text = re.sub(r"\r+\n", "\n", text)
    return tuple(line.rstrip("\r") for line in text.splitlines())


def _extract_outputs(
    lines: tuple[str, ...], commands: tuple[DebugConsoleCommand, ...], nonce: str
) -> dict[str, str]:
    observed_nonces = set(_PROTOCOL_NONCE_RE.findall("\n".join(lines)))
    if observed_nonces - {nonce}:
        raise DebugConsoleProtocolError(
            "serial transcript contains stale nonce markers"
        )

    expected_protocol_lines = (
        {command.begin_marker for command in commands}
        | {command.end_marker for command in commands}
        | {f"{command.status_prefix}0" for command in commands}
    )
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
        values = [
            index
            for index, line in enumerate(lines)
            if line.startswith(command.value_prefix)
        ]
        ends = [index for index, line in enumerate(lines) if line == command.end_marker]
        if len(begins) != 1 or len(values) != 1 or len(statuses) != 1 or len(ends) != 1:
            raise DebugConsoleProtocolError(
                f"{command.name} command markers are missing or duplicated"
            )
        begin, value, status, end = begins[0], values[0], statuses[0], ends[0]
        if not previous_end < begin < value < status < end:
            raise DebugConsoleProtocolError(
                "debug-console command markers are reordered"
            )
        if any(
            ord(character) < 0x20 and character != "\t" or ord(character) == 0x7F
            for line in (lines[begin], lines[value], lines[status], lines[end])
            for character in line
        ):
            raise DebugConsoleProtocolError(
                f"{command.name} command frame contains control input"
            )
        if lines[status] != f"{command.status_prefix}0":
            raise DebugConsoleProtocolError(
                f"{command.name} command returned a nonzero or invalid status"
            )
        value_line = lines[value]
        expected_protocol_lines.add(value_line)
        outputs[command.name] = value_line.removeprefix(command.value_prefix)
        previous_end = end

    for line in lines:
        if line.startswith(f"__ASTERINAS_DEBUG_{nonce}_") and (
            line not in expected_protocol_lines
        ):
            raise DebugConsoleProtocolError("serial transcript has an unknown marker")
    return outputs


def classify_debug_console(transcript: str | bytes, nonce: str) -> DebugConsoleEvidence:
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

    attempt_nonce = nonce
    for attempt in range(DEBUG_CONSOLE_ATTEMPTS):
        commands = debug_console_commands(attempt_nonce)
        phase_start = serial.checkpoint()
        try:
            for command in commands:
                command_start = serial.checkpoint()
                command_timeout = (
                    DEBUG_CONSOLE_GRAPHICAL_TIMEOUT
                    if command.name == "graphical"
                    else DEBUG_CONSOLE_COMMAND_TIMEOUT
                )
                command_deadline = min(deadline, time.monotonic() + command_timeout)
                serial.send(f"{command.payload}\n".encode(), command_deadline)
                serial.wait_for_any(
                    (
                        f"{command.begin_marker}\r".encode(),
                        f"{command.begin_marker}\n".encode(),
                    ),
                    command_deadline,
                    start=command_start,
                )
                serial.wait_for_any(
                    (
                        f"{command.end_marker}\r".encode(),
                        f"{command.end_marker}\n".encode(),
                    ),
                    command_deadline,
                    start=command_start,
                )
        except TimeoutError:
            if attempt + 1 == DEBUG_CONSOLE_ATTEMPTS:
                raise
            now = time.monotonic()
            if now >= deadline:
                raise
            serial.send(b"\x03\n", min(deadline, now + DEBUG_CONSOLE_ABORT_TIMEOUT))
            attempt_nonce = hashlib.sha256(
                f"{nonce}:{attempt + 1}".encode()
            ).hexdigest()[:32]
            continue
        return classify_debug_console(serial.transcript[phase_start:], attempt_nonce)
    raise AssertionError("debug-console attempts exhausted")
