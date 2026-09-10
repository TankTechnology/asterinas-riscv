#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and validate the bounded Megrez physical graphics interaction gate."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import secrets
import select
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TextIO

from tools.riscv.debian.rootfs.debug_console_protocol import (
    DEBUG_CONSOLE_READY,
    run_debug_console_phase,
)
from tools.riscv.debian.rootfs.physical_graphics_gate import (
    GateError as GuestGateError,
    validate_png_screenshot,
)
from tools.riscv.debian.rootfs.gate_runtime import (
    PinnedOutputDirectory,
    SerialConsole,
)
from tools.riscv.megrez_board_session import (
    MEGREZ_FRAMEBUFFER,
    MEGREZ_USB_HOST_COMMAND,
    BoardSession,
    open_serial,
    safe_artifact_name,
    validate_debug_console_readiness,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debug_board import (
    BOARD_ARTIFACT_NAMES,
    BoardTransport,
    _lock_serial,
    _uboot_bootargs_commands,
)
from tools.riscv.megrez_debug_contract import DebugPlan
from tools.riscv.megrez_debug_simulation import _validate_current_artifacts


MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_HDMI_BYTES = 64 * 1024 * 1024
MAX_GUEST_SCREENSHOT_BYTES = 512 * 1024
PHYSICAL_REBOOT_AFTER = 900
PHYSICAL_REBOOT_HEADROOM = 30.0
PHYSICAL_MARIONETTE_SETUP_TIMEOUT = 300.0
_NONCE = re.compile(r"[0-9a-f]{16}")
_SHA256 = r"[0-9a-f]{64}"
PHYSICAL_EXTERNAL_MARKER = "__ASTERINAS_PHYSICAL_EXTERNAL__"
_EXTERNAL_SERVICES_QUIESCED = re.compile(
    rf"{PHYSICAL_EXTERNAL_MARKER} status=([0-9]+) setup_status=([0-9]+) "
    r"evidence_state=([a-z-]+) evidence_pid=([0-9]+) "
    r"network_state=([a-z-]+) network_pid=([0-9]+)"
)
_READY = re.compile(
    rf"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=([1-3]) nonce_sha256=({_SHA256})"
)
_KEY_READY = re.compile(
    rf"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle=([1-3]) "
    rf"nonce_sha256=({_SHA256})"
)
_POINTER_READY = re.compile(r"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle=([1-3])")
_INPUT = re.compile(
    rf"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle=([1-3]) "
    rf"key_downs=([0-9]+) relative_events=([0-9]+) "
    rf"absolute_events=([0-9]+) left_down=([0-9]+) "
    rf"left_up=([0-9]+) digest=({_SHA256})"
)
_DOM = re.compile(
    rf"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle=([1-3]) "
    rf"nonce_sha256=({_SHA256}) trusted_key=1 trusted_input=1 "
    rf"trusted_pointer=1 trusted_click=1 click_count=1 color=cyan"
)
_SCREENSHOT = re.compile(
    rf"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle=([1-3]) sha256=({_SHA256})"
)
_PASS = re.compile(r"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=([1-3])")
_COMPLETE = re.compile(r"ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=([13])")
_SCREENSHOT_BEGIN = re.compile(
    rf"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=([1-3]) "
    rf"size=([0-9]+) sha256=({_SHA256})"
)
_SCREENSHOT_END = re.compile(r"__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=([1-3])")
_FINAL = re.compile(
    rf"__ASTERINAS_PHYSICAL_FINAL__ cycle=([13]) nonce_sha256=({_SHA256})"
)
_PREFLIGHT = re.compile(
    r"__ASTERINAS_PHYSICAL_PREFLIGHT__ browser_pid=([0-9]+) "
    r"input_nodes=([0-9]+) framebuffer=([01]) xorg_fbdev=([01]) "
    r"openbox=([01]) firefox=([01]) browser_service=([a-z-]+) "
    r"browser_restarts=([0-9]+) usb_inputs=([0-9]+) "
    r"usb_keyboard=([01]) usb_mouse=([01])"
)
_PROTOCOL_PREFIX = "ASTERINAS_PHYSICAL_GRAPHICS_"
_FATAL_MARKERS = (
    "kernel panic",
    "not syncing",
    "oops:",
    "fatal exception",
    "out of memory",
    "killed process",
    "failed to resolve usb host",
    "rejected selected usb host",
    "failed to retain selected xhci",
    "xhci capability probe failed",
    "failed to select dwc3 host",
    "xhci hid startup failed",
    "usb hid transfer stopped",
    "failed to allocate usb irq",
    "irq chip unavailable for usb",
    "failed to map usb interrupt",
    "failed to register exclusive usb irq",
    "failed to enable xhci interrupts",
    "usb irq rearm rejected",
    "failed to restore disabled xhci interrupts",
    "failed to disable xhci interrupts",
    "invalid usb boot mouse report",
    "invalid usb boot keyboard report",
    "failed to map firmware framebuffer",
    "firmware framebuffer is not synchronizable",
    "framebuffer corruption",
)


class HostGateError(RuntimeError):
    """A failure that prevents publishing physical graphics evidence."""


class PointerEvidenceMode(str, Enum):
    """Select the exact evdev motion contract for one execution environment."""

    PHYSICAL_RELATIVE = "physical-relative"
    QEMU_TABLET = "qemu-tablet"


class DisplayEvidenceMode(str, Enum):
    """Select the independent physical-display evidence source."""

    EXTERNAL_HDMI = "external-hdmi"
    OPERATOR_ATTESTED = "operator-attested"


@dataclass(frozen=True)
class InteractionCycleEvidence:
    """Nonce-bound evidence for one real keyboard and pointer cycle."""

    cycle: int
    nonce_sha256: str
    key_downs: int
    relative_events: int
    absolute_events: int
    left_down: int
    left_up: int
    evdev_sha256: str
    screenshot_sha256: str


@dataclass(frozen=True)
class FileEvidence:
    """Identity of one retained, bounded display capture."""

    path: Path
    size: int
    sha256: str
    format: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or type(self.size) is not int
            or not 0 < self.size <= MAX_HDMI_BYTES
            or re.fullmatch(_SHA256, self.sha256) is None
            or self.format not in ("png", "jpg")
        ):
            raise HostGateError("file evidence identity is invalid")


@dataclass(frozen=True)
class OperatorDisplayEvidence:
    """Nonce-bound live operator observation of the physical final display."""

    kind: str
    nonce_sha256: str
    state: str
    confirmed: bool

    def __post_init__(self) -> None:
        if (
            self.kind != "operator-attested"
            or re.fullmatch(_SHA256, self.nonce_sha256) is None
            or self.state != "cyan-final-cycle-pass"
            or self.confirmed is not True
        ):
            raise HostGateError("operator display evidence is invalid")


@dataclass(frozen=True)
class GraphicalReadinessEvidence:
    """Exact userspace processes and devices required before operator input."""

    browser_pid: int
    input_nodes: int
    framebuffer: bool
    xorg_fbdev: bool
    openbox: bool
    firefox: bool
    browser_service: str
    browser_restarts: int
    usb_inputs: int
    usb_keyboard: bool
    usb_mouse: bool

    def __post_init__(self) -> None:
        if (
            type(self.browser_pid) is not int
            or self.browser_pid <= 1
            or type(self.input_nodes) is not int
            or self.input_nodes < 2
            or any(
                value is not True
                for value in (
                    self.framebuffer,
                    self.xorg_fbdev,
                    self.openbox,
                    self.firefox,
                    self.usb_keyboard,
                    self.usb_mouse,
                )
            )
            or self.browser_service != "active"
            or type(self.browser_restarts) is not int
            or self.browser_restarts != 0
            or type(self.usb_inputs) is not int
            or self.usb_inputs != 2
        ):
            raise HostGateError("graphical readiness contract is incomplete")


@dataclass(frozen=True)
class PhysicalGraphicsConfig:
    """Independent bounded deadlines for one physical acceptance attempt."""

    open_timeout: float = 60.0
    artifact_timeout: float = 300.0
    boot_timeout: float = 180.0
    cycle_timeout: float = 180.0
    hdmi_timeout: float = 180.0
    recovery_timeout: float = 930.0
    cycles_requested: int = 3
    display_mode: DisplayEvidenceMode = DisplayEvidenceMode.EXTERNAL_HDMI

    def __post_init__(self) -> None:
        values = (
            self.open_timeout,
            self.artifact_timeout,
            self.boot_timeout,
            self.cycle_timeout,
            self.hdmi_timeout,
            self.recovery_timeout,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1200
            for value in values
        ):
            raise ValueError("physical graphics deadlines must be in (0, 1200]")
        if self.cycle_timeout > 300:
            raise ValueError("guest cycle timeout must be at most 300 seconds")
        if type(self.cycles_requested) is not int or self.cycles_requested not in (
            1,
            3,
        ):
            raise ValueError("physical cycle count must be one or three")
        if not isinstance(self.display_mode, DisplayEvidenceMode):
            raise ValueError("display evidence mode is invalid")


@dataclass(frozen=True)
class PhysicalGraphicsResult:
    """Canonical result published only after the board lifecycle terminates."""

    schema_version: int
    cycles_requested: int
    passed: bool
    physical: bool
    reason: str
    plan_sha256: str
    bootargs_sha256: str
    recovered: bool
    readiness: GraphicalReadinessEvidence | None
    cycles: tuple[InteractionCycleEvidence, ...]
    hdmi: FileEvidence | None
    operator_display: OperatorDisplayEvidence | None
    transport: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.physical is not True:
            raise HostGateError("physical result cannot describe a simulated run")
        if self.schema_version != 2:
            raise HostGateError("physical result schema version must be two")
        if type(self.cycles_requested) is not int or self.cycles_requested not in (
            1,
            3,
        ):
            raise HostGateError("physical result cycle count must be one or three")
        if len(self.cycles) > self.cycles_requested:
            raise HostGateError("physical result has excess interaction cycles")
        if self.hdmi is not None and self.operator_display is not None:
            raise HostGateError("physical result has conflicting display evidence")
        if self.passed:
            if len(self.cycles) != self.cycles_requested:
                raise HostGateError("passing result has an incomplete cycle set")
            if self.reason == "physical-graphics-pass":
                valid_display = self.hdmi is not None and self.operator_display is None
            elif self.reason == "physical-graphics-operator-attested-pass":
                valid_display = self.hdmi is None and self.operator_display is not None
                if valid_display and (
                    not self.cycles
                    or self.operator_display.nonce_sha256
                    != self.cycles[-1].nonce_sha256
                ):
                    raise HostGateError(
                        "operator display evidence is not bound to the final cycle"
                    )
            else:
                valid_display = False
            if not valid_display:
                raise HostGateError("passing result lacks matching display evidence")

    def canonical_bytes(self) -> bytes:
        document = asdict(self)
        if self.hdmi is not None:
            document["hdmi"]["path"] = self.hdmi.path.name  # type: ignore[index]
        return (
            json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()


class PhysicalGraphicsOperations(Protocol):
    """Injected board and evidence operations used by the lifecycle."""

    @property
    def transcript(self) -> str | bytes: ...

    @property
    def guest_started(self) -> bool: ...

    def invalidate(self) -> None: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]: ...

    def boot(self, plan: DebugPlan, bootargs: str, timeout: float) -> None: ...

    def prove_graphical_readiness(
        self, timeout: float
    ) -> GraphicalReadinessEvidence: ...

    def run_cycle(self, cycle: int, nonce: str, timeout: float) -> bytes: ...

    def retain_hdmi(self, timeout: float) -> FileEvidence: ...

    def retain_operator_display(
        self, nonce: str, timeout: float
    ) -> OperatorDisplayEvidence: ...

    def prove_final_state(
        self,
        cycle: int,
        nonce: str,
        readiness: GraphicalReadinessEvidence,
        timeout: float,
    ) -> None: ...

    def emit_complete(self, cycles_requested: int, timeout: float) -> None: ...

    def await_recovery(self, timeout: float) -> None: ...

    def publish(
        self,
        result: PhysicalGraphicsResult,
        screenshots: tuple[bytes, ...],
        hdmi: FileEvidence | None,
        outcomes: tuple[str, ...],
    ) -> None: ...

    def close(self) -> None: ...


def _decode_transcript(transcript: str | bytes) -> str:
    if isinstance(transcript, str):
        try:
            payload = transcript.encode("utf-8")
        except UnicodeEncodeError as error:
            raise HostGateError("transcript is not valid UTF-8") from error
        text = transcript
    elif isinstance(transcript, bytes):
        payload = transcript
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HostGateError("transcript is not valid UTF-8") from error
    else:
        raise HostGateError("transcript must be text or bytes")
    if len(payload) > MAX_TRANSCRIPT_BYTES:
        raise HostGateError("transcript exceeds 8 MiB")
    return text


def _validated_nonce_hashes(nonces: Sequence[str]) -> tuple[str, ...]:
    if (
        isinstance(nonces, (str, bytes))
        or len(nonces) not in (1, 3)
        or any(
            not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            for nonce in nonces
        )
        or len(set(nonces)) != len(nonces)
    ):
        raise HostGateError(
            "expected one or three distinct 16-digit lowercase hex nonces"
        )
    return tuple(hashlib.sha256(nonce.encode()).hexdigest() for nonce in nonces)


def _validated_sha256_identities(
    nonce_hashes: Sequence[str],
) -> tuple[str, ...]:
    if (
        isinstance(nonce_hashes, (str, bytes))
        or len(nonce_hashes) not in (1, 3)
        or any(
            not isinstance(identity, str) or re.fullmatch(_SHA256, identity) is None
            for identity in nonce_hashes
        )
        or len(set(nonce_hashes)) != len(nonce_hashes)
    ):
        raise HostGateError("expected one or three distinct SHA-256 nonce identities")
    return tuple(nonce_hashes)


def _match(line: str, pattern: re.Pattern[str], label: str) -> re.Match[str]:
    match = pattern.fullmatch(line)
    if match is None:
        raise HostGateError(f"invalid or out-of-order {label} marker")
    return match


def classify_interaction_transcript(
    transcript: str | bytes,
    nonces: Sequence[str],
    *,
    pointer_mode: PointerEvidenceMode = PointerEvidenceMode.PHYSICAL_RELATIVE,
) -> tuple[InteractionCycleEvidence, ...]:
    """Require one or three exact, ordered, nonce-bound interaction cycles."""

    return _classify_interaction_hash_transcript(
        transcript,
        _validated_nonce_hashes(nonces),
        pointer_mode=pointer_mode,
    )


def classify_interaction_hash_transcript(
    transcript: str | bytes,
    nonce_hashes: Sequence[str],
    *,
    pointer_mode: PointerEvidenceMode = PointerEvidenceMode.PHYSICAL_RELATIVE,
) -> tuple[InteractionCycleEvidence, ...]:
    """Validate the same protocol from already-bound nonce identities."""

    return _classify_interaction_hash_transcript(
        transcript,
        _validated_sha256_identities(nonce_hashes),
        pointer_mode=pointer_mode,
    )


def _classify_interaction_hash_transcript(
    transcript: str | bytes,
    nonce_hashes: tuple[str, ...],
    *,
    pointer_mode: PointerEvidenceMode,
) -> tuple[InteractionCycleEvidence, ...]:
    """Implement the shared exact marker protocol after identity validation."""

    if not isinstance(pointer_mode, PointerEvidenceMode):
        raise HostGateError("pointer evidence mode is invalid")
    text = _decode_transcript(transcript)
    lowered = text.lower()
    for marker in _FATAL_MARKERS:
        if marker in lowered:
            raise HostGateError(f"fatal serial marker: {marker}")

    protocol_lines = tuple(
        line.rstrip("\r")
        for line in text.splitlines()
        if line.rstrip("\r").startswith(_PROTOCOL_PREFIX)
    )
    if any(line.startswith(f"{_PROTOCOL_PREFIX}FAIL") for line in protocol_lines):
        raise HostGateError("guest physical graphics gate reported failure")
    expected_markers = len(nonce_hashes) * 7 + 1
    if len(protocol_lines) != expected_markers:
        raise HostGateError(
            f"expected exactly {expected_markers} physical graphics markers"
        )

    evidence: list[InteractionCycleEvidence] = []
    offset = 0
    for cycle, nonce_sha256 in enumerate(nonce_hashes, start=1):
        ready = _match(protocol_lines[offset], _READY, "READY")
        key_ready = _match(protocol_lines[offset + 1], _KEY_READY, "KEY_READY")
        pointer_ready = _match(
            protocol_lines[offset + 2], _POINTER_READY, "POINTER_READY"
        )
        input_event = _match(protocol_lines[offset + 3], _INPUT, "INPUT")
        dom = _match(protocol_lines[offset + 4], _DOM, "DOM")
        screenshot = _match(protocol_lines[offset + 5], _SCREENSHOT, "SCREENSHOT")
        passed = _match(protocol_lines[offset + 6], _PASS, "PASS")
        offset += 7

        marker_cycles = (
            int(ready.group(1)),
            int(key_ready.group(1)),
            int(pointer_ready.group(1)),
            int(input_event.group(1)),
            int(dom.group(1)),
            int(screenshot.group(1)),
            int(passed.group(1)),
        )
        if marker_cycles != (cycle,) * 7:
            raise HostGateError(
                f"cycle {cycle} markers do not share the expected cycle"
            )
        if (
            ready.group(2) != nonce_sha256
            or key_ready.group(2) != nonce_sha256
            or dom.group(2) != nonce_sha256
        ):
            raise HostGateError(f"cycle {cycle} nonce hash mismatch")

        key_downs = int(input_event.group(2))
        relative_events = int(input_event.group(3))
        absolute_events = int(input_event.group(4))
        left_down = int(input_event.group(5))
        left_up = int(input_event.group(6))
        motion_complete = (
            relative_events >= 1
            if pointer_mode is PointerEvidenceMode.PHYSICAL_RELATIVE
            else absolute_events >= 1
        )
        if key_downs < 16 or not motion_complete or (left_down, left_up) != (1, 1):
            raise HostGateError(
                f"cycle {cycle} has insufficient physical input evidence"
            )
        evidence.append(
            InteractionCycleEvidence(
                cycle=cycle,
                nonce_sha256=nonce_sha256,
                key_downs=key_downs,
                relative_events=relative_events,
                absolute_events=absolute_events,
                left_down=left_down,
                left_up=left_up,
                evdev_sha256=input_event.group(7),
                screenshot_sha256=screenshot.group(2),
            )
        )

    complete = _match(protocol_lines[offset], _COMPLETE, "COMPLETE")
    if int(complete.group(1)) != len(nonce_hashes):
        raise HostGateError("completion marker cycle count mismatch")
    if len({cycle.screenshot_sha256 for cycle in evidence}) != len(evidence):
        raise HostGateError("cycle screenshot digests are not distinct")
    return tuple(evidence)


def physical_bootargs(plan: DebugPlan | Any) -> str:
    """Derive the one ephemeral debug-console boot from a frozen browser plan."""

    plan.validate()
    if not isinstance(plan.bootargs, str):
        raise HostGateError("plan bootargs are invalid")
    tokens = plan.bootargs.split()
    if tokens.count("--") != 1:
        raise HostGateError("plan must contain one root-init separator")
    separator = tokens.index("--")
    if tokens[separator + 1 :] != ["--root-init=systemd"]:
        raise HostGateError("plan root-init arguments are not canonical")
    replaced_kernel_parameters = {
        "console",
        "loglevel",
        "asterinas.klog_capture",
        "asterinas.mmc_write_partition2",
        "asterinas.reboot_after",
    }
    retained = []
    for token in tokens[:separator]:
        normalized_name = token.partition("=")[0].replace("-", "_")
        if normalized_name in replaced_kernel_parameters or token.startswith(
            (
                "systemd.unit=",
                "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=",
            )
        ):
            continue
        retained.append(token)
    if retained.count("init=/init") != 1:
        raise HostGateError("plan must contain one stage1 init selector")
    physical = (
        "console=tty0",
        "loglevel=off",
        "asterinas.klog_capture=info",
        *retained,
        f"asterinas.reboot_after={PHYSICAL_REBOOT_AFTER}",
        "systemd.mask=asterinas-browser-web-evidence.service",
        "systemd.mask=asterinas-desktop-m5-network.service",
        "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
        "--",
        "--root-init=systemd",
        "--debug-console=isolated-root",
    )
    return " ".join(physical)


def physical_preflight_command() -> str:
    """Return the fixed root-shell probe for the physical graphics surface."""

    return (
        "_asterinas_physical_input_nodes=0; "
        "for _asterinas_physical_node in /dev/input/event*; do "
        '[ -c "$_asterinas_physical_node" ] && '
        "_asterinas_physical_input_nodes=$((_asterinas_physical_input_nodes + 1)); "
        "done; "
        "_asterinas_physical_framebuffer=0; [ -c /dev/fb0 ] && "
        "_asterinas_physical_framebuffer=1; "
        "set -- $(/usr/bin/python3 -c 'import fcntl,glob,struct;"
        'q=lambda p,c,n:(lambda b:(fcntl.ioctl(open(p,"rb",buffering=0),c,b),'
        "bytes(b))[1])(bytearray(n));"
        'd=[(struct.unpack("=HHHH",q(p,0x80084502,8))[0],'
        'q(p,0x81004506,256).split(b"\\0",1)[0],'
        'q(p,0x81004507,256).split(b"\\0",1)[0]) for p in '
        'glob.glob("/dev/input/event*")];'
        'k=(3,b"usb_boot_keyboard",b"xhci/input0");'
        'm=(3,b"usb_boot_mouse",b"xhci/input1");'
        "print(sum(x in (k,m) for x in d),int(k in d),int(m in d))' "
        "2>/dev/null || printf '0 0 0'); "
        "_asterinas_physical_usb_inputs=${1:-0}; "
        "_asterinas_physical_usb_keyboard=${2:-0}; "
        "_asterinas_physical_usb_mouse=${3:-0}; "
        "_asterinas_physical_xorg=0; "
        "for _asterinas_physical_xorg_pid in $(pgrep -x Xorg 2>/dev/null); do "
        "for _asterinas_physical_xorg_fd in "
        "/proc/$_asterinas_physical_xorg_pid/fd/*; do "
        '[ "$(readlink "$_asterinas_physical_xorg_fd" 2>/dev/null)" = /dev/fb0 ] && '
        "[ -S /tmp/.X11-unix/X0 ] && _asterinas_physical_xorg=1; "
        "done; done; "
        "_asterinas_physical_openbox=0; "
        "pgrep -u 1000 -x openbox >/dev/null 2>&1 && _asterinas_physical_openbox=1; "
        "_asterinas_physical_service=$(systemctl is-active "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_pid=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_restarts=$(systemctl show --property NRestarts --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_firefox=0; "
        "case $_asterinas_physical_pid in ''|*[!0-9]*) ;; *) "
        "grep -Eq '^firefox(-esr)?$' \"/proc/$_asterinas_physical_pid/comm\" "
        "2>/dev/null && _asterinas_physical_firefox=1 ;; esac; "
        "printf '__ASTERINAS_PHYSICAL_PREFLIGHT__ browser_pid=%s input_nodes=%s "
        "framebuffer=%s xorg_fbdev=%s openbox=%s firefox=%s browser_service=%s "
        "browser_restarts=%s usb_inputs=%s usb_keyboard=%s usb_mouse=%s\\n' "
        '"$_asterinas_physical_pid" '
        '"$_asterinas_physical_input_nodes" "$_asterinas_physical_framebuffer" '
        '"$_asterinas_physical_xorg" "$_asterinas_physical_openbox" '
        '"$_asterinas_physical_firefox" "$_asterinas_physical_service" '
        '"$_asterinas_physical_restarts" "$_asterinas_physical_usb_inputs" '
        '"$_asterinas_physical_usb_keyboard" "$_asterinas_physical_usb_mouse"'
    )


def physical_external_services_quiesce_command() -> str:
    """Prepare volatile graphics state and stop competing external workloads."""

    return (
        "_asterinas_external_status=0; "
        "_asterinas_control=/run/systemd/system.control; "
        "_asterinas_home=/run/asterinas-physical-home; "
        "_asterinas_browser=asterinas-browser-web.service; "
        '/usr/bin/install -d -m 0755 "$_asterinas_control" '
        "|| _asterinas_external_status=$?; "
        "/usr/bin/install -d -m 0700 -o 1000 -g 1000 "
        '"$_asterinas_home" "$_asterinas_home/.mozilla" '
        '"$_asterinas_home/.mozilla/asterinas-browser-web" '
        '"$_asterinas_home/.cache" "$_asterinas_home/.config" '
        '"$_asterinas_home/Downloads" '
        "|| _asterinas_external_status=$?; "
        "/usr/bin/install -m 0600 -o 1000 -g 1000 /dev/null "
        '"$_asterinas_home/browser-web-timeline.log" '
        "|| _asterinas_external_status=$?; "
        "/usr/bin/install -d -m 0755 "
        '"$_asterinas_control/$_asterinas_browser.d" '
        "|| _asterinas_external_status=$?; "
        "printf '%s\\n' '[Service]' "
        "'Environment=HOME=/run/asterinas-physical-home' "
        "'Environment=ASTERINAS_WEB_NETWORK_MODE=proxy' "
        "'Environment=XDG_CACHE_HOME=/run/asterinas-physical-home/.cache' "
        '>"$_asterinas_control/$_asterinas_browser.d/physical.conf" '
        "|| _asterinas_external_status=$?; "
        "for _asterinas_external_unit in "
        "asterinas-browser-web-evidence.service "
        "asterinas-desktop-m5-network.service "
        "serial-getty@ttyS0.service console-getty.service; do "
        "/usr/bin/ln -sfn /dev/null "
        '"$_asterinas_control/$_asterinas_external_unit" '
        "|| _asterinas_external_status=$?; done; "
        "/usr/bin/timeout 15 /usr/bin/systemctl daemon-reload >/dev/null 2>&1 "
        "|| _asterinas_external_status=$?; "
        "/usr/bin/timeout 60 /usr/bin/systemctl stop "
        "asterinas-desktop-m5.service "
        "asterinas-browser-web.service "
        "asterinas-browser-web-evidence.service "
        "asterinas-desktop-m5-network.service "
        "serial-getty@ttyS0.service console-getty.service >/dev/null 2>&1 "
        "|| _asterinas_external_status=$?; "
        "/usr/bin/mountpoint -q /home/asterinas || "
        '/usr/bin/mount --bind "$_asterinas_home" /home/asterinas '
        "|| _asterinas_external_status=$?; "
        "/usr/bin/systemctl reset-failed "
        "asterinas-desktop-m5.service "
        "asterinas-browser-web-timeline-basic.service "
        "asterinas-browser-web.service "
        "asterinas-browser-web-evidence.service "
        "asterinas-desktop-m5-network.service >/dev/null 2>&1 || true; "
        "/usr/bin/systemctl start --no-block asterinas-desktop-m5.service "
        ">/dev/null 2>&1 || _asterinas_external_status=$?; "
        "/usr/bin/timeout 15 /usr/bin/systemctl start "
        "asterinas-browser-web-timeline-basic.service >/dev/null 2>&1 "
        "|| _asterinas_external_status=$?; "
        "/usr/bin/systemctl start --no-block asterinas-browser-web.service "
        ">/dev/null 2>&1 || _asterinas_external_status=$?; "
        "/usr/bin/systemctl start --no-block graphical.target >/dev/null 2>&1 "
        "|| _asterinas_external_status=$?; "
        "/usr/bin/sleep 1; "
        "_asterinas_evidence_state=$(/usr/bin/systemctl is-active "
        "asterinas-browser-web-evidence.service 2>/dev/null || true); "
        "_asterinas_evidence_pid=$(/usr/bin/systemctl show --property MainPID "
        "--value asterinas-browser-web-evidence.service 2>/dev/null || true); "
        "_asterinas_network_state=$(/usr/bin/systemctl is-active "
        "asterinas-desktop-m5-network.service 2>/dev/null || true); "
        "_asterinas_network_pid=$(/usr/bin/systemctl show --property MainPID "
        "--value asterinas-desktop-m5-network.service 2>/dev/null || true); "
        "_asterinas_terminal_status=0; "
        '[ "$_asterinas_evidence_state" = inactive ] && '
        '[ "$_asterinas_evidence_pid" = 0 ] && '
        '[ "$_asterinas_network_state" = inactive ] && '
        '[ "$_asterinas_network_pid" = 0 ] '
        "|| _asterinas_terminal_status=124; "
        'case "$_asterinas_external_status" in 0|124) ;; *) '
        '_asterinas_terminal_status="$_asterinas_external_status" ;; esac; '
        f"printf '{PHYSICAL_EXTERNAL_MARKER} status=%s setup_status=%s "
        "evidence_state=%s evidence_pid=%s network_state=%s network_pid=%s\n' "
        '"$_asterinas_terminal_status" "$_asterinas_external_status" '
        '"$_asterinas_evidence_state" '
        '"$_asterinas_evidence_pid" "$_asterinas_network_state" '
        '"$_asterinas_network_pid"'
    )


def validate_physical_external_services_quiesced(line: str) -> None:
    """Accept only a complete, inactive state for both competing services."""

    match = _EXTERNAL_SERVICES_QUIESCED.fullmatch(line)
    if match is None:
        raise HostGateError("external service state is malformed")
    (
        status,
        setup_status,
        evidence_state,
        evidence_pid,
        network_state,
        network_pid,
    ) = match.groups()
    if (
        status != "0"
        or setup_status not in ("0", "124")
        or evidence_state != "inactive"
        or evidence_pid != "0"
        or network_state != "inactive"
        or network_pid != "0"
    ):
        raise HostGateError("external services are still active")


def physical_cycle_command(
    cycle: int,
    nonce: str,
    timeout: float,
    *,
    expected_browser_pid: int | None = None,
    expected_width: int = 1920,
    expected_height: int = 1080,
    setup_timeout: float = PHYSICAL_MARIONETTE_SETUP_TIMEOUT,
) -> str:
    """Return one root command that observes, but cannot synthesize, input."""

    if type(cycle) is not int or cycle not in (1, 2, 3):
        raise ValueError("physical cycle must be 1, 2, or 3")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError("physical nonce must be 16 lowercase hex digits")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValueError("physical cycle timeout must be in (0, 300]")
    if (
        isinstance(setup_timeout, bool)
        or not isinstance(setup_timeout, (int, float))
        or not math.isfinite(setup_timeout)
        or not 0 < setup_timeout <= 900
    ):
        raise ValueError("physical setup timeout must be in (0, 900]")
    if expected_browser_pid is not None and (
        type(expected_browser_pid) is not int or expected_browser_pid <= 1
    ):
        raise ValueError("expected Firefox PID is outside the valid contract")
    if (
        type(expected_width) is not int
        or type(expected_height) is not int
        or not 0 < expected_width <= 16384
        or not 0 < expected_height <= 16384
    ):
        raise ValueError("expected screenshot dimensions are outside the contract")
    expected_pid_check = (
        ""
        if expected_browser_pid is None
        else (
            f'[ "$_asterinas_physical_pid" = {expected_browser_pid} ] || '
            "_asterinas_physical_status=124; "
        )
    )
    return (
        "_asterinas_physical_pid=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_status=125; "
        "case $_asterinas_physical_pid in ''|*[!0-9]*) ;; *) "
        f"{expected_pid_check}"
        'if [ "$_asterinas_physical_status" != 124 ]; then '
        f'nsenter -t "$_asterinas_physical_pid" -n '
        f"/usr/lib/asterinas/physical-graphics-gate --nonce {nonce} "
        f'--cycle {cycle} --firefox-pid "$_asterinas_physical_pid" '
        f"--timeout {timeout:g} --setup-timeout {setup_timeout:g} "
        f"--expected-width {expected_width} "
        f"--expected-height {expected_height}; "
        "_asterinas_physical_status=$?; fi ;; esac; "
        "_asterinas_physical_current=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_restarts=$(systemctl show --property NRestarts --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        '[ "$_asterinas_physical_current" = "$_asterinas_physical_pid" ] && '
        '[ "$_asterinas_physical_restarts" = 0 ] || _asterinas_physical_status=126; '
        f"printf '__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle={cycle} status=%s\\n' "
        '"$_asterinas_physical_status"'
    )


def physical_final_command(
    nonce: str,
    browser_pid: int,
    timeout: float,
    *,
    cycle: int = 3,
    setup_timeout: float = PHYSICAL_MARIONETTE_SETUP_TIMEOUT,
) -> str:
    """Return a read-only terminal-DOM check bound to the original Firefox PID."""

    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError("physical nonce must be 16 lowercase hex digits")
    if type(browser_pid) is not int or browser_pid <= 1:
        raise ValueError("expected Firefox PID is outside the valid contract")
    if type(cycle) is not int or cycle not in (1, 3):
        raise ValueError("physical final cycle must be one or three")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValueError("physical final timeout must be in (0, 300]")
    if (
        isinstance(setup_timeout, bool)
        or not isinstance(setup_timeout, (int, float))
        or not math.isfinite(setup_timeout)
        or not 0 < setup_timeout <= 900
    ):
        raise ValueError("physical setup timeout must be in (0, 900]")
    return (
        "_asterinas_physical_pid=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_status=124; "
        f'if [ "$_asterinas_physical_pid" = {browser_pid} ]; then '
        f'nsenter -t "$_asterinas_physical_pid" -n '
        f"/usr/lib/asterinas/physical-graphics-gate --nonce {nonce} "
        f'--cycle {cycle} --firefox-pid "$_asterinas_physical_pid" --verify-final '
        f"--timeout {timeout:g} --setup-timeout {setup_timeout:g}; "
        "_asterinas_physical_status=$?; fi; "
        "_asterinas_physical_current=$(systemctl show --property MainPID --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        "_asterinas_physical_restarts=$(systemctl show --property NRestarts --value "
        "asterinas-browser-web.service 2>/dev/null || true); "
        '[ "$_asterinas_physical_current" = "$_asterinas_physical_pid" ] && '
        '[ "$_asterinas_physical_restarts" = 0 ] || _asterinas_physical_status=126; '
        "printf '__ASTERINAS_PHYSICAL_FINAL_STATUS__ status=%s\\n' "
        '"$_asterinas_physical_status"'
    )


def extract_screenshot_frame(
    transcript: str | bytes,
    cycle: int,
    *,
    expected_dimensions: tuple[int, int] = (1920, 1080),
) -> bytes:
    """Decode one exact serial screenshot frame and bind its size and digest."""

    if type(cycle) is not int or cycle not in (1, 2, 3):
        raise ValueError("physical cycle must be 1, 2, or 3")
    text = _decode_transcript(transcript)
    # Asterinas' serial console can echo CR before QEMU appends its own CRLF,
    # producing ``\r\r\n``.  ``str.splitlines`` treats both CR characters as
    # independent boundaries and invents blank records inside the three-line
    # frame.  Split only on the transport's LF delimiter, then normalize every
    # preceding CR.
    lines = tuple(line.rstrip("\r") for line in text.split("\n"))
    begins = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _SCREENSHOT_BEGIN.fullmatch(line)) is not None
    ]
    ends = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _SCREENSHOT_END.fullmatch(line)) is not None
    ]
    if len(begins) != 1 or len(ends) != 1:
        raise HostGateError("screenshot frame is missing or duplicated")
    begin_index, begin = begins[0]
    end_index, end = ends[0]
    if (
        int(begin.group(1)) != cycle
        or int(end.group(1)) != cycle
        or end_index != begin_index + 2
    ):
        raise HostGateError("screenshot frame cycle or ordering mismatch")
    expected_size = int(begin.group(2))
    if not 8 < expected_size <= MAX_GUEST_SCREENSHOT_BYTES:
        raise HostGateError("screenshot frame size is outside the serial contract")
    try:
        payload = base64.b64decode(lines[begin_index + 1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise HostGateError("screenshot frame is not canonical base64") from error
    if len(payload) != expected_size or hashlib.sha256(
        payload
    ).hexdigest() != begin.group(3):
        raise HostGateError("screenshot frame payload identity mismatch")
    try:
        validate_png_screenshot(payload, expected_dimensions=expected_dimensions)
    except GuestGateError as error:
        raise HostGateError(f"screenshot frame PNG is invalid: {error}") from error
    return payload


def _failure_reason(error: BaseException) -> str:
    detail = re.sub(r"[^a-z0-9]+", "-", str(error).lower()).strip("-")[:160]
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", type(error).__name__).lower()
    return f"{name}:{detail or 'unspecified'}"


def _result(
    plan: DebugPlan | Any,
    bootargs: str,
    *,
    passed: bool,
    reason: str,
    recovered: bool,
    readiness: GraphicalReadinessEvidence | None,
    cycles: tuple[InteractionCycleEvidence, ...],
    hdmi: FileEvidence | None,
    operator_display: OperatorDisplayEvidence | None,
    transport: tuple[str, ...],
    cycles_requested: int,
    display_mode: DisplayEvidenceMode,
) -> PhysicalGraphicsResult:
    expected_reason = (
        "physical-graphics-pass"
        if display_mode is DisplayEvidenceMode.EXTERNAL_HDMI
        else "physical-graphics-operator-attested-pass"
    )
    if passed and (
        reason != expected_reason
        or not recovered
        or readiness is None
        or len(cycles) != cycles_requested
        or (
            display_mode is DisplayEvidenceMode.EXTERNAL_HDMI
            and (hdmi is None or operator_display is not None)
        )
        or (
            display_mode is DisplayEvidenceMode.OPERATOR_ATTESTED
            and (operator_display is None or hdmi is not None)
        )
    ):
        raise HostGateError("passing result lacks terminal physical evidence")
    return PhysicalGraphicsResult(
        schema_version=2,
        cycles_requested=cycles_requested,
        passed=passed,
        physical=True,
        reason=reason,
        plan_sha256=plan.plan_sha256,
        bootargs_sha256=hashlib.sha256(bootargs.encode()).hexdigest(),
        recovered=recovered,
        readiness=readiness,
        cycles=cycles,
        hdmi=hdmi,
        operator_display=operator_display,
        transport=transport,
    )


def run_physical_graphics(
    plan: DebugPlan | Any,
    config: PhysicalGraphicsConfig,
    operations: PhysicalGraphicsOperations,
    *,
    nonces: Sequence[str] | None = None,
    artifact_validator: Callable[
        [DebugPlan | Any], object
    ] = _validate_current_artifacts,
) -> PhysicalGraphicsResult:
    """Execute the requested real-input cycles and require fresh U-Boot recovery."""

    plan.validate()
    artifact_validator(plan)
    bootargs = physical_bootargs(plan)
    selected_nonces = (
        tuple(secrets.token_hex(8) for _ in range(config.cycles_requested))
        if nonces is None
        else tuple(nonces)
    )
    _validated_nonce_hashes(selected_nonces)
    if len(selected_nonces) != config.cycles_requested:
        raise HostGateError("nonce count does not match requested cycle count")

    outcomes: tuple[str, ...] = ()
    readiness: GraphicalReadinessEvidence | None = None
    retained_screenshots: list[bytes] = []
    cycles: tuple[InteractionCycleEvidence, ...] = ()
    hdmi: FileEvidence | None = None
    operator_display: OperatorDisplayEvidence | None = None
    recovered = False
    failure: BaseException | None = None
    interruption: BaseException | None = None
    try:
        try:
            operations.invalidate()
            operations.open(config.open_timeout)
            outcomes = operations.ensure_artifacts(plan, config.artifact_timeout)
            operations.boot(plan, bootargs, config.boot_timeout)
            readiness = operations.prove_graphical_readiness(config.boot_timeout)

            for cycle, nonce in enumerate(selected_nonces, start=1):
                payload = operations.run_cycle(cycle, nonce, config.cycle_timeout)
                if (
                    not isinstance(payload, bytes)
                    or not 8 < len(payload) <= MAX_GUEST_SCREENSHOT_BYTES
                ):
                    raise HostGateError(f"cycle {cycle} screenshot is invalid")
                try:
                    validate_png_screenshot(payload)
                except GuestGateError as error:
                    raise HostGateError(
                        f"cycle {cycle} screenshot is structurally invalid: {error}"
                    ) from error
                retained_screenshots.append(payload)

            if config.display_mode is DisplayEvidenceMode.EXTERNAL_HDMI:
                hdmi = operations.retain_hdmi(config.hdmi_timeout)
            else:
                operator_display = operations.retain_operator_display(
                    selected_nonces[-1], config.hdmi_timeout
                )
            if readiness is None:
                raise HostGateError(
                    "graphical readiness disappeared before final check"
                )
            operations.prove_final_state(
                config.cycles_requested,
                selected_nonces[-1],
                readiness,
                config.cycle_timeout,
            )
            operations.emit_complete(config.cycles_requested, config.cycle_timeout)
        except Exception as error:
            failure = error
        except BaseException as error:
            interruption = error

        if operations.guest_started:
            try:
                operations.await_recovery(config.recovery_timeout)
                recovered = True
            except Exception as recovery_error:
                if failure is None:
                    failure = recovery_error
                else:
                    failure = HostGateError(
                        f"{_failure_reason(failure)}; recovery="
                        f"{_failure_reason(recovery_error)}"
                    )
            except BaseException as recovery_interruption:
                if interruption is None:
                    interruption = recovery_interruption

        if interruption is not None:
            raise interruption

        screenshots = tuple(retained_screenshots)
        if failure is None:
            try:
                cycles = classify_interaction_transcript(
                    operations.transcript, selected_nonces
                )
                for cycle, payload in zip(cycles, screenshots):
                    if cycle.screenshot_sha256 != hashlib.sha256(payload).hexdigest():
                        raise HostGateError(
                            f"cycle {cycle.cycle} screenshot payload hash mismatch"
                        )
            except Exception as error:
                failure = error

        if failure is None:
            result = _result(
                plan,
                bootargs,
                passed=True,
                reason=(
                    "physical-graphics-pass"
                    if config.display_mode is DisplayEvidenceMode.EXTERNAL_HDMI
                    else "physical-graphics-operator-attested-pass"
                ),
                recovered=recovered,
                readiness=readiness,
                cycles=cycles,
                hdmi=hdmi,
                operator_display=operator_display,
                transport=outcomes,
                cycles_requested=config.cycles_requested,
                display_mode=config.display_mode,
            )
        else:
            result = _result(
                plan,
                bootargs,
                passed=False,
                reason=_failure_reason(failure),
                recovered=recovered,
                readiness=readiness,
                cycles=cycles,
                hdmi=hdmi,
                operator_display=operator_display,
                transport=outcomes,
                cycles_requested=config.cycles_requested,
                display_mode=config.display_mode,
            )
        operations.publish(result, screenshots, hdmi, outcomes)
        return result
    finally:
        operations.close()


def _read_held_regular(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HostGateError(f"cannot open HDMI evidence: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise HostGateError("HDMI evidence is not a regular file")
        if not 0 < before.st_size <= MAX_HDMI_BYTES:
            raise HostGateError("HDMI evidence size is outside 1..64 MiB")
        payload = bytearray()
        while len(payload) <= MAX_HDMI_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_HDMI_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(payload) != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise HostGateError("HDMI evidence changed while being read")
        try:
            current = path.stat(follow_symlinks=False)
        except OSError as error:
            raise HostGateError(
                "HDMI evidence path changed while being read"
            ) from error
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise HostGateError("HDMI evidence path was replaced while being read")
        return bytes(payload), before
    finally:
        os.close(descriptor)


def _image_format(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            validate_png_screenshot(
                payload,
                max_bytes=MAX_HDMI_BYTES,
                expected_dimensions=None,
            )
        except GuestGateError as error:
            raise HostGateError(f"HDMI PNG is structurally invalid: {error}") from error
        return "png"
    if payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9"):
        return "jpg"
    raise HostGateError("HDMI evidence is neither PNG nor JPEG")


def _publish_new_private_file(destination: Path, payload: bytes) -> None:
    if destination.name in ("", ".", ".."):
        raise HostGateError("invalid HDMI evidence destination")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(destination.parent, directory_flags)
    except OSError as error:
        raise HostGateError(f"cannot open HDMI evidence directory: {error}") from error
    temporary = f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        try:
            os.stat(destination.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HostGateError("HDMI evidence destination already exists")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise HostGateError("short write while retaining HDMI evidence")
            written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except HostGateError:
        raise
    except OSError as error:
        raise HostGateError(f"cannot retain HDMI evidence: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def ingest_hdmi(source: Path | str, destination: Path | str) -> FileEvidence:
    """Validate and atomically retain one operator-supplied HDMI capture."""

    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_absolute() or not destination_path.is_absolute():
        raise HostGateError("HDMI source and destination must be absolute")
    try:
        if source_path.absolute() == destination_path.absolute():
            raise HostGateError("HDMI source and destination must differ")
        destination_status = destination_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        destination_status = None
    except OSError as error:
        raise HostGateError(f"cannot inspect HDMI evidence paths: {error}") from error

    payload, source_status = _read_held_regular(source_path)
    if destination_status is not None and (
        destination_status.st_dev,
        destination_status.st_ino,
    ) == (source_status.st_dev, source_status.st_ino):
        raise HostGateError("HDMI destination aliases its source")
    image_format = _image_format(payload)
    _publish_new_private_file(destination_path, payload)
    return FileEvidence(
        path=destination_path,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        format=image_format,
    )


def _remaining(deadline: float, *, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{phase} deadline expired")
    return remaining


def _wait_text_readable(stream: TextIO, timeout: float) -> bool:
    """Wait until a text stream can provide input without blocking."""

    readable, _writable, _exceptional = select.select((stream,), (), (), timeout)
    return bool(readable)


def _read_operator_confirmation(
    expected: str,
    timeout: float,
    *,
    stream: TextIO | None = None,
    wait_readable: Callable[[TextIO, float], bool] = _wait_text_readable,
) -> None:
    """Accept one exact, newline-terminated operator confirmation."""

    if re.fullmatch(r"confirm-cyan-pass [0-9a-f]{8}", expected) is None:
        raise ValueError("operator confirmation challenge is invalid")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 1200
    ):
        raise ValueError("operator confirmation timeout must be in (0, 1200]")
    selected_stream = sys.stdin if stream is None else stream
    deadline = time.monotonic() + timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not wait_readable(selected_stream, remaining):
        raise TimeoutError("operator display confirmation timed out")
    line = selected_stream.readline(len(expected) + 2)
    if line == "":
        raise HostGateError("operator display confirmation reached EOF")
    if line != expected + "\n":
        raise HostGateError("operator display confirmation did not match exactly")
    if wait_readable(selected_stream, 0.0) and selected_stream.read(1) != "":
        raise HostGateError("operator display confirmation contained extra input")


def _safe_output_directory(path: Path, repository: Path) -> Path:
    repository = repository.absolute()
    allowed = repository / "target" / "current-main-physical-graphics" / "physical"
    candidate = path.absolute()
    try:
        candidate.relative_to(allowed)
    except ValueError as error:
        raise HostGateError(
            "physical output must be under "
            "target/current-main-physical-graphics/physical"
        ) from error
    current = repository
    for component in candidate.relative_to(repository).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HostGateError("physical output path contains an unsafe component")
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    candidate.chmod(0o700)
    return candidate


class RealPhysicalGraphicsOperations:
    """Descriptor-owned physical board, serial protocol, and evidence adapter."""

    GUEST_LIFETIME_SECONDS = PHYSICAL_REBOOT_AFTER

    _OUTPUT_NAMES = (
        "result.json",
        "physical.serial.log",
        "transport.json",
        "sha256sums.txt",
        "physical-graphics-cycle-1.png",
        "physical-graphics-cycle-2.png",
        "physical-graphics-cycle-3.png",
        "hdmi-evidence.png",
        "hdmi-evidence.jpg",
        "operator-display-attestation.json",
    )

    def __init__(
        self,
        plan: DebugPlan,
        device: str,
        output_directory: Path,
        hdmi_capture: Path | None,
        *,
        display_mode: DisplayEvidenceMode = DisplayEvidenceMode.EXTERNAL_HDMI,
        cycles_requested: int = 3,
        confirmation_reader: Callable[[str, float], None] = _read_operator_confirmation,
        repository: Path | None = None,
        open_device: Callable[[str], int] = open_serial,
        lock_device: Callable[[int], None] = _lock_serial,
        close_device: Callable[[int], None] = os.close,
        session_factory: Callable[..., BoardSession] = BoardSession.from_fd,
        mmc_artifacts: Mapping[str, str] | None = None,
    ) -> None:
        self._plan = plan
        self._device = device
        self._output_path = output_directory
        self._hdmi_capture = hdmi_capture
        if not isinstance(display_mode, DisplayEvidenceMode):
            raise ValueError("display evidence mode is invalid")
        if type(cycles_requested) is not int or cycles_requested not in (1, 3):
            raise ValueError("physical cycle count must be one or three")
        if display_mode is DisplayEvidenceMode.EXTERNAL_HDMI:
            if not isinstance(hdmi_capture, Path):
                raise ValueError("external HDMI mode requires a capture path")
        elif hdmi_capture is not None:
            raise ValueError("operator-attested mode cannot accept an HDMI path")
        self._display_mode = display_mode
        self._cycles_requested = cycles_requested
        self._confirmation_reader = confirmation_reader
        self._repository = (
            repository.absolute()
            if repository is not None
            else Path(__file__).resolve().parents[2]
        )
        self._open_device = open_device
        self._lock_device = lock_device
        self._close_device = close_device
        self._session_factory = session_factory
        self._mmc_artifacts = None if mmc_artifacts is None else dict(mmc_artifacts)
        self._output: PinnedOutputDirectory | None = None
        self._fd: int | None = None
        self._session: BoardSession | None = None
        self._serial: SerialConsole | None = None
        self._log: TextIO = io.StringIO()
        self._logged_serial_bytes = 0
        self._browser_pid: int | None = None
        self._guest_started = False
        self._guest_deadline: float | None = None

    @property
    def transcript(self) -> str:
        self._sync_serial_log()
        return self._log.getvalue()

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    def invalidate(self) -> None:
        self._guest_started = False
        self._guest_deadline = None
        output_path = _safe_output_directory(self._output_path, self._repository)
        if self._hdmi_capture is not None:
            try:
                self._hdmi_capture.absolute().relative_to(output_path)
            except ValueError:
                pass
            else:
                raise HostGateError(
                    "HDMI capture source must be outside the output directory"
                )
        self._output = PinnedOutputDirectory(output_path)
        self._output.invalidate(*self._OUTPUT_NAMES)

    @staticmethod
    def _hdmi_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def open(self, timeout: float) -> None:
        fd = self._open_device(self._device)
        try:
            self._lock_device(fd)
            session = self._session_factory(
                fd,
                None,
                confirm=False,
                final_marker=DEBUG_CONSOLE_READY,
                log_stream=self._log,
            )
            session.send("")
            session.wait_for_uboot_prompt(timeout)
        except BaseException:
            self._close_device(fd)
            raise
        self._fd = fd
        self._session = session

    def _require_session(self) -> tuple[BoardSession, int]:
        if self._session is None or self._fd is None:
            raise HostGateError("physical serial session is not open")
        return self._session, self._fd

    def _require_serial(self) -> SerialConsole:
        if self._serial is None:
            raise HostGateError("physical guest serial protocol is not ready")
        return self._serial

    def _require_output(self) -> PinnedOutputDirectory:
        if self._output is None:
            raise HostGateError("physical output directory is not pinned")
        return self._output

    def _sync_serial_log(self) -> None:
        if self._serial is None:
            return
        transcript = self._serial.transcript
        if len(transcript) < self._logged_serial_bytes:
            raise HostGateError("serial transcript moved backwards")
        payload = transcript[self._logged_serial_bytes :]
        if payload:
            self._log.write(payload.decode("utf-8", errors="replace"))
            self._logged_serial_bytes = len(transcript)

    @staticmethod
    def _next_line(
        serial: SerialConsole, cursor: int, deadline: float
    ) -> tuple[str, int]:
        while True:
            transcript = serial.transcript
            newline = transcript.find(b"\n", cursor)
            if newline >= 0:
                try:
                    line = transcript[cursor:newline].rstrip(b"\r").decode("utf-8")
                except UnicodeDecodeError as error:
                    raise HostGateError("serial protocol line is not UTF-8") from error
                return line, newline + 1
            serial.wait_for(b"\n", deadline, start=cursor)

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]:
        session, fd = self._require_session()
        deadline = time.monotonic() + timeout
        identities = {identity.name: identity for identity in plan.artifacts}
        if self._mmc_artifacts is not None:
            outcomes = []
            for name in BOARD_ARTIFACT_NAMES:
                _remaining(deadline, phase="MMC artifact load")
                identity = identities[name]
                actual_size = session.load_artifact(
                    name,
                    self._mmc_artifacts[name],
                    identity.load_address,
                    identity.crc32,
                )
                if actual_size != identity.size:
                    raise HostGateError(
                        f"{name}: MMC size mismatch: expected {identity.size}, "
                        f"got {actual_size}"
                    )
                outcomes.append(f"{name}:mmc")
            return tuple(outcomes)
        transport = BoardTransport(
            fd=fd,
            command=lambda command, budget: session.command(command, timeout=budget),
        )
        outcomes = []
        for name in BOARD_ARTIFACT_NAMES:
            outcome = transport.ensure(
                identities[name],
                timeout=_remaining(deadline, phase="artifact transfer"),
            )
            outcomes.append(f"{outcome.artifact}:{outcome.status}")
        return tuple(outcomes)

    def boot(self, plan: DebugPlan, bootargs: str, timeout: float) -> None:
        session, fd = self._require_session()
        deadline = time.monotonic() + timeout
        identities = {identity.name: identity for identity in plan.artifacts}
        initramfs = identities["initramfs"]
        commands = (
            "mmc dev 1",
            "mmc rescan",
            "fdt addr 0xf0000000",
            "fdt resize 0x1000",
            *MEGREZ_FRAMEBUFFER.commands(),
            f"setenv initrd_size 0x{initramfs.size:x}",
            *_uboot_bootargs_commands(bootargs),
            MEGREZ_USB_HOST_COMMAND,
        )
        for command in commands:
            session.command(
                command,
                timeout=_remaining(deadline, phase="U-Boot graphics preparation"),
            )
        kernel = identities["kernel"]
        dtb = identities["megrez_dtb"]
        # The kernel's recovery timer is fixed, not renewed by a new cycle.
        # Start slightly earlier on the host and leave room to drain evidence.
        self._guest_deadline = (
            time.monotonic() + self.GUEST_LIFETIME_SECONDS - PHYSICAL_REBOOT_HEADROOM
        )
        session.start_boot_attempt()
        session.send(
            f"booti 0x{kernel.load_address:x} "
            f"0x{initramfs.load_address:x}:0x{initramfs.size:x} "
            f"0x{dtb.load_address:x}"
        )
        self._guest_started = True
        self._serial = SerialConsole(
            fd,
            max_bytes=MAX_TRANSCRIPT_BYTES,
            tx_delay=0.005,
        )

    def _guest_phase_deadline(self, timeout: float) -> float:
        if self._guest_deadline is None:
            raise HostGateError("physical guest lifetime was not established")
        now = time.monotonic()
        deadline = min(now + timeout, self._guest_deadline)
        if deadline <= now:
            raise TimeoutError("physical guest reboot budget exhausted")
        return deadline

    def prove_graphical_readiness(self, timeout: float) -> GraphicalReadinessEvidence:
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        serial.wait_for(DEBUG_CONSOLE_READY.encode(), deadline)
        validate_debug_console_readiness(serial.transcript.decode("utf-8"))
        self._quiesce_external_services(deadline)

        last_error: HostGateError | None = None
        while True:
            try:
                evidence = self._probe_graphical_readiness(deadline)
            except HostGateError as error:
                last_error = error
            else:
                run_debug_console_phase(
                    serial,
                    deadline,
                    secrets.token_hex(16),
                    ready_seen=True,
                )
                self._browser_pid = evidence.browser_pid
                self._sync_serial_log()
                return evidence
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last_error or HostGateError("graphical preflight timed out")
            time.sleep(min(1.0, remaining))

    def _quiesce_external_services(self, deadline: float) -> None:
        serial = self._require_serial()
        cursor = serial.checkpoint()
        serial.send(
            (physical_external_services_quiesce_command() + "\n").encode(), deadline
        )
        while True:
            line, cursor = self._next_line(serial, cursor, deadline)
            if not line.startswith(PHYSICAL_EXTERNAL_MARKER):
                continue
            validate_physical_external_services_quiesced(line)
            return

    def _probe_graphical_readiness(self, deadline: float) -> GraphicalReadinessEvidence:
        serial = self._require_serial()
        cursor = serial.checkpoint()
        serial.send((physical_preflight_command() + "\n").encode(), deadline)
        while True:
            line, cursor = self._next_line(serial, cursor, deadline)
            match = _PREFLIGHT.fullmatch(line)
            if match is None:
                continue
            return GraphicalReadinessEvidence(
                browser_pid=int(match.group(1)),
                input_nodes=int(match.group(2)),
                framebuffer=match.group(3) == "1",
                xorg_fbdev=match.group(4) == "1",
                openbox=match.group(5) == "1",
                firefox=match.group(6) == "1",
                browser_service=match.group(7),
                browser_restarts=int(match.group(8)),
                usb_inputs=int(match.group(9)),
                usb_keyboard=match.group(10) == "1",
                usb_mouse=match.group(11) == "1",
            )

    def run_cycle(self, cycle: int, nonce: str, timeout: float) -> bytes:
        serial = self._require_serial()
        if self._browser_pid is None:
            raise HostGateError("Firefox readiness was not established")
        deadline = self._guest_phase_deadline(
            PHYSICAL_MARIONETTE_SETUP_TIMEOUT + timeout + 90.0
        )
        cycle_start = serial.checkpoint()
        cursor = cycle_start
        command = physical_cycle_command(
            cycle,
            nonce,
            timeout,
            expected_browser_pid=self._browser_pid,
        )
        serial.send((command + "\n").encode(), deadline)
        nonce_sha256 = hashlib.sha256(nonce.encode()).hexdigest()
        ready = (
            f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} "
            f"nonce_sha256={nonce_sha256}"
        )
        passed = f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}"
        status = f"__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle={cycle} status=0"
        ready_seen = False
        passed_seen = False
        while not passed_seen:
            line, cursor = self._next_line(serial, cursor, deadline)
            if line == ready:
                if ready_seen:
                    raise HostGateError(f"cycle {cycle} duplicated READY")
                ready_seen = True
                print(
                    f"[physical cycle {cycle}/{self._cycles_requested}] "
                    f"type nonce {nonce}, then move "
                    "the USB mouse and click the amber button",
                    flush=True,
                )
            elif line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL"):
                raise HostGateError(f"cycle {cycle} guest failure: {line}")
            elif line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise HostGateError(f"cycle {cycle} command exited before PASS: {line}")
            elif line == passed:
                if not ready_seen:
                    raise HostGateError(f"cycle {cycle} PASS preceded READY")
                passed_seen = True
        while True:
            line, cursor = self._next_line(serial, cursor, deadline)
            if line == status:
                break
            if line.startswith("__ASTERINAS_PHYSICAL_COMMAND_STATUS__"):
                raise HostGateError(f"cycle {cycle} command returned nonzero")
        segment = serial.transcript[cycle_start:cursor]
        payload = extract_screenshot_frame(segment, cycle)
        self._sync_serial_log()
        return payload

    def retain_hdmi(self, timeout: float) -> FileEvidence:
        if (
            self._display_mode is not DisplayEvidenceMode.EXTERNAL_HDMI
            or self._hdmi_capture is None
        ):
            raise HostGateError("external HDMI evidence mode is not selected")
        output = self._require_output()
        deadline = self._guest_phase_deadline(timeout)
        try:
            before_prompt = self._hdmi_capture.stat(follow_symlinks=False)
        except FileNotFoundError:
            initial_identity = None
        else:
            initial_identity = self._hdmi_identity(before_prompt)
        print(
            f"[physical HDMI] capture the cyan final-cycle page to {self._hdmi_capture}",
            flush=True,
        )
        candidate_identity: tuple[int, ...] | None = None
        stable_since = 0.0
        while True:
            try:
                payload, metadata = _read_held_regular(self._hdmi_capture)
                identity = self._hdmi_identity(metadata)
                if identity == initial_identity:
                    raise HostGateError("HDMI capture is stale")
                image_format = _image_format(payload)
            except HostGateError as error:
                cause = error.__cause__
                message = str(error)
                retryable = (
                    "HDMI capture is stale" in message
                    or "HDMI PNG is structurally invalid" in message
                    or "neither PNG nor JPEG" in message
                    or "changed while being read" in message
                    or "path was replaced while being read" in message
                    or isinstance(cause, FileNotFoundError)
                )
                if "size is outside 1..64 MiB" in message:
                    try:
                        current = self._hdmi_capture.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        retryable = True
                    else:
                        retryable = (
                            stat.S_ISREG(current.st_mode) and current.st_size == 0
                        )
                if not retryable:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HDMI capture was not supplied") from error
                time.sleep(min(0.25, remaining))
                continue
            now = time.monotonic()
            if identity != candidate_identity:
                candidate_identity = identity
                stable_since = now
            if now - stable_since < 0.5:
                remaining = deadline - now
                if remaining <= 0:
                    raise TimeoutError("HDMI capture did not settle")
                time.sleep(min(0.25, remaining))
                continue
            break
        name = f"hdmi-evidence.{image_format}"
        output.atomic_write(name, payload, mode=0o600)
        return FileEvidence(
            path=output.path / name,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            format=image_format,
        )

    def retain_operator_display(
        self, nonce: str, timeout: float
    ) -> OperatorDisplayEvidence:
        if self._display_mode is not DisplayEvidenceMode.OPERATOR_ATTESTED:
            raise HostGateError("operator-attested display mode is not selected")
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise ValueError("physical nonce must be 16 lowercase hex digits")
        deadline = self._guest_phase_deadline(timeout)
        expected = f"confirm-cyan-pass {nonce[-8:]}"
        print(
            "[physical display] confirm that the physical monitor shows the "
            f"cyan final-cycle PASS page; type exactly: {expected}",
            flush=True,
        )
        self._confirmation_reader(
            expected,
            _remaining(deadline, phase="operator display confirmation"),
        )
        return OperatorDisplayEvidence(
            kind="operator-attested",
            nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
            state="cyan-final-cycle-pass",
            confirmed=True,
        )

    def prove_final_state(
        self,
        cycle: int,
        nonce: str,
        readiness: GraphicalReadinessEvidence,
        timeout: float,
    ) -> None:
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(
            PHYSICAL_MARIONETTE_SETUP_TIMEOUT + timeout + 30.0
        )
        final_readiness = self._probe_graphical_readiness(deadline)
        if final_readiness != readiness:
            raise HostGateError("graphical state changed during HDMI capture")
        cursor = serial.checkpoint()
        serial.send(
            (
                physical_final_command(
                    nonce, readiness.browser_pid, timeout, cycle=cycle
                )
                + "\n"
            ).encode(),
            deadline,
        )
        expected_hash = hashlib.sha256(nonce.encode()).hexdigest()
        final_seen = False
        status_seen = False
        while not status_seen:
            line, cursor = self._next_line(serial, cursor, deadline)
            match = _FINAL.fullmatch(line)
            if match is not None:
                if (
                    final_seen
                    or match.group(1) != str(cycle)
                    or match.group(2) != expected_hash
                ):
                    raise HostGateError(
                        "terminal DOM marker is duplicated or mismatched"
                    )
                final_seen = True
            elif line == "__ASTERINAS_PHYSICAL_FINAL_STATUS__ status=0":
                status_seen = True
            elif line.startswith("ASTERINAS_PHYSICAL_GRAPHICS_FAIL") or line.startswith(
                "__ASTERINAS_PHYSICAL_FINAL_STATUS__"
            ):
                raise HostGateError("terminal DOM verification failed")
        if not final_seen:
            raise HostGateError("terminal DOM marker is missing")
        self._sync_serial_log()

    def emit_complete(self, cycles_requested: int, timeout: float) -> None:
        if type(cycles_requested) is not int or cycles_requested not in (1, 3):
            raise ValueError("physical cycle count must be one or three")
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        cursor = serial.checkpoint()
        marker = f"ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles={cycles_requested}"
        serial.send((f"printf '{marker}\\n'\n").encode(), deadline)
        while True:
            line, cursor = self._next_line(serial, cursor, deadline)
            if line == marker:
                self._sync_serial_log()
                return

    def await_recovery(self, timeout: float) -> None:
        session, _fd = self._require_session()
        self._sync_serial_log()
        recovery = session.wait_for_uboot_prompt(timeout)
        validate_recovery_epoch(recovery)

    def publish(
        self,
        result: PhysicalGraphicsResult,
        screenshots: tuple[bytes, ...],
        hdmi: FileEvidence | None,
        outcomes: tuple[str, ...],
    ) -> None:
        output = self._require_output()
        output_names: list[str] = []
        output.atomic_write(
            "physical.serial.log", self.transcript.encode("utf-8"), mode=0o600
        )
        output_names.append("physical.serial.log")
        transport = (
            json.dumps(
                {
                    "schema_version": 1,
                    "plan_sha256": self._plan.plan_sha256,
                    "outcomes": list(outcomes),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        output.atomic_write("transport.json", transport, mode=0o600)
        output_names.append("transport.json")
        for cycle, payload in enumerate(screenshots, start=1):
            name = f"physical-graphics-cycle-{cycle}.png"
            output.atomic_write(name, payload, mode=0o600)
            output_names.append(name)
        if hdmi is not None:
            if hdmi.path.parent.absolute() != output.path.absolute():
                raise HostGateError("retained HDMI path escaped the output directory")
            if output.sha256(hdmi.path.name) != hdmi.sha256:
                raise HostGateError("retained HDMI digest changed before publication")
            output_names.append(hdmi.path.name)
        if result.operator_display is not None:
            attestation = (
                json.dumps(
                    asdict(result.operator_display),
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            output.atomic_write(
                "operator-display-attestation.json", attestation, mode=0o600
            )
            output_names.append("operator-display-attestation.json")

        result_payload = result.canonical_bytes()

        sums = [
            f"{identity.sha256}  plan-artifact-{identity.name}"
            for identity in self._plan.artifacts
        ]
        sums.extend(f"{output.sha256(name)}  {name}" for name in output_names)
        sums.append(f"{hashlib.sha256(result_payload).hexdigest()}  result.json")
        output.atomic_write(
            "sha256sums.txt", ("\n".join(sums) + "\n").encode(), mode=0o600
        )
        output.atomic_write("result.json", result_payload, mode=0o600)

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            self._session = None
            self._serial = None
            self._close_device(fd)
        if self._output is not None:
            self._output.close()
            self._output = None


def _read_plan(path: Path) -> DebugPlan:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HostGateError(f"cannot open debug plan: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= 1024 * 1024
        ):
            raise HostGateError("debug plan is not a bounded regular file")
        payload = bytearray()
        while len(payload) <= 1024 * 1024:
            chunk = os.read(descriptor, 1024 * 1024 + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size:
            raise HostGateError("debug plan changed while being read")
    finally:
        os.close(descriptor)
    try:
        plan = DebugPlan.from_bytes(bytes(payload))
    except ValueError as error:
        raise HostGateError(f"debug plan is invalid: {error}") from error
    if plan.schema_version != 2 or plan.profile != "debian-browser":
        raise HostGateError("physical graphics requires a Debian browser plan")
    return plan


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be numeric") from error
    if not math.isfinite(seconds) or not 0 < seconds <= 1200:
        raise argparse.ArgumentTypeError("timeout must be in (0, 1200]")
    return seconds


def parse_args(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    display = parser.add_mutually_exclusive_group(required=True)
    display.add_argument("--hdmi-capture", type=Path)
    display.add_argument("--operator-display-attestation", action="store_true")
    parser.add_argument("--cycles", type=int, choices=(1, 3), default=3)
    parser.add_argument("--mmc-kernel", type=safe_artifact_name)
    parser.add_argument("--mmc-initramfs", type=safe_artifact_name)
    parser.add_argument("--mmc-dtb", type=safe_artifact_name)
    parser.add_argument("--open-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--artifact-timeout", type=_positive_seconds, default=300.0)
    parser.add_argument("--boot-timeout", type=_positive_seconds, default=180.0)
    parser.add_argument("--cycle-timeout", type=_positive_seconds, default=180.0)
    parser.add_argument("--hdmi-timeout", type=_positive_seconds, default=180.0)
    parser.add_argument("--recovery-timeout", type=_positive_seconds, default=930.0)
    values = parser.parse_args(arguments)
    mmc_names = (values.mmc_kernel, values.mmc_initramfs, values.mmc_dtb)
    if any(name is not None for name in mmc_names) and not all(
        name is not None for name in mmc_names
    ):
        parser.error(
            "--mmc-kernel, --mmc-initramfs, and --mmc-dtb must be supplied together"
        )
    return values


def main(arguments: Sequence[str] | None = None) -> int:
    values = parse_args(sys.argv[1:] if arguments is None else arguments)
    try:
        plan = _read_plan(values.plan)
        config = PhysicalGraphicsConfig(
            open_timeout=values.open_timeout,
            artifact_timeout=values.artifact_timeout,
            boot_timeout=values.boot_timeout,
            cycle_timeout=values.cycle_timeout,
            hdmi_timeout=values.hdmi_timeout,
            recovery_timeout=values.recovery_timeout,
            cycles_requested=values.cycles,
            display_mode=(
                DisplayEvidenceMode.OPERATOR_ATTESTED
                if values.operator_display_attestation
                else DisplayEvidenceMode.EXTERNAL_HDMI
            ),
        )
        mmc_artifacts = (
            None
            if values.mmc_kernel is None
            else {
                "kernel": values.mmc_kernel,
                "initramfs": values.mmc_initramfs,
                "megrez_dtb": values.mmc_dtb,
            }
        )
        operations = RealPhysicalGraphicsOperations(
            plan,
            values.device,
            values.output_directory,
            values.hdmi_capture,
            display_mode=config.display_mode,
            cycles_requested=config.cycles_requested,
            mmc_artifacts=mmc_artifacts,
        )
        result = run_physical_graphics(plan, config, operations)
    except (HostGateError, OSError, RuntimeError, ValueError) as error:
        print(
            f"physical graphics gate failed before publication: {error}",
            file=sys.stderr,
        )
        return 2
    print(result.canonical_bytes().decode(), end="")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
