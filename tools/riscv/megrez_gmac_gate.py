#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot and classify the physical Megrez wired-network milestone."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_CORE_MILESTONES,
    DESKTOP_M4_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_MEGREZ_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DESKTOP_M6_REMOTE_MARKER,
)
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import (
    DESKTOP_M7_HOME_MARKER,
    DESKTOP_M7_READY_MARKER,
    DESKTOP_M7_SEARCH_MARKER,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_board_session import (
    BoardSession,
    boot_loaded_artifacts,
    crc32_value,
    parse_expected_crc32,
    positive_finite_seconds,
    positive_size,
    read_available,
    safe_artifact_name,
    safe_ipv4,
    safe_ipv4_netmask,
)
from tools.riscv.megrez_network_fixture import (
    FIXTURE_PATH,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
    is_successful_summary,
)


BOARD_ADDRESS = "10.100.19.200"
HOST_ADDRESS = "10.100.19.216"
HOST_HARDWARE_ADDRESS = "04:7c:16:47:50:4e"
GATEWAY_ADDRESS = "10.100.16.1"
GATEWAY_HARDWARE_ADDRESS = "4c:d6:29:18:93:43"
PROXY_PORT = 17893
PROXY_URL = f"http://{HOST_ADDRESS}:{PROXY_PORT}"
FIXTURE_PORT = 17894
FIXTURE_REQUESTS = 20
FIXTURE_URL = f"http://{HOST_ADDRESS}:{FIXTURE_PORT}{FIXTURE_PATH}"
NETWORK_BOOTARG = f"asterinas.net=eic7700-rj45,{BOARD_ADDRESS}/21,{GATEWAY_ADDRESS}"
ROOTFS_WRITE_BOOTARG = "asterinas.mmc_write_partition2"
NEIGHBOR_BOOTARGS = " ".join(
    f"asterinas.neighbor=eic7700-rj45,{address},{hardware_address}"
    for address, hardware_address in (
        (GATEWAY_ADDRESS, GATEWAY_HARDWARE_ADDRESS),
        (HOST_ADDRESS, HOST_HARDWARE_ADDRESS),
    )
)
SERIAL_EVIDENCE_VARIABLES = (
    "ASTERINAS_DESKTOP_M4_CONSOLE",
    "ASTERINAS_DESKTOP_M5_CONSOLE",
    "ASTERINAS_BROWSER_M6_CONSOLE",
)
DESKTOP_MASK_BOOTARGS = " ".join(
    f"systemd.mask={service}"
    for service in (
        "asterinas-desktop-m5-network.service",
        "asterinas-desktop-m4-evidence.service",
        "asterinas-desktop-m6-browser.service",
        "asterinas-desktop-m7-baidu.service",
        "asterinas-desktop-m8-browser-quality.service",
    )
)
MAX_UBOOT_COMMAND_BYTES = 1024
DESKTOP_PROXY_BOOTARGS = " ".join(
    f"systemd.setenv={name}={value}"
    for name, value in (
        ("ASTERINAS_DESKTOP_PROXY_URL", PROXY_URL),
        ("ASTERINAS_DESKTOP_PROXY_HOST", HOST_ADDRESS),
        ("ASTERINAS_DESKTOP_PROXY_PORT", str(PROXY_PORT)),
    )
)
DESKTOP_FIXTURE_BOOTARGS = " ".join(
    f"systemd.setenv={name}={value}"
    for name, value in (
        ("ASTERINAS_DESKTOP_FIXTURE_URL", FIXTURE_URL),
        ("ASTERINAS_DESKTOP_FIXTURE_SIZE", str(PAYLOAD_SIZE)),
        ("ASTERINAS_DESKTOP_FIXTURE_SHA256", PAYLOAD_SHA256),
        ("ASTERINAS_DESKTOP_FIXTURE_REQUESTS", str(FIXTURE_REQUESTS)),
    )
)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
PHYSICAL_MILESTONES = (
    b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 ",
    *(marker.encode() for marker in DESKTOP_M5_MEGREZ_MILESTONES),
    DESKTOP_M4_MILESTONES[-1].encode(),
    DESKTOP_M6_REMOTE_MARKER.encode(),
)
PHYSICAL_NETWORK_MILESTONES = (
    b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 ",
    *(marker.encode() for marker in DESKTOP_M5_MEGREZ_MILESTONES),
)
PHYSICAL_DESKTOP_MILESTONES = tuple(
    marker.encode() for marker in DESKTOP_M4_CORE_MILESTONES
)
PHYSICAL_M7_MILESTONES = (
    DESKTOP_M7_HOME_MARKER.encode(),
    DESKTOP_M7_SEARCH_MARKER.encode(),
    DESKTOP_M7_READY_MARKER.encode(),
)
_BROWSER_JAVASCRIPT_RE = re.compile(
    rb"DEBIAN_BROWSER_M6_JAVASCRIPT status=(limited-pass|disabled|failed)"
)
_BROWSER_READY_RE = re.compile(
    rb"DEBIAN_BROWSER_M6_READY remote=baidu javascript=(limited-pass|disabled|failed)"
)
PHYSICAL_READY_MARKERS = (DESKTOP_M7_READY_MARKER.encode(),)
PHYSICAL_NETWORK_READY_MARKERS = (PHYSICAL_NETWORK_MILESTONES[-1],)
PHYSICAL_DESKTOP_READY_MARKERS = (PHYSICAL_DESKTOP_MILESTONES[-1],)
_FATAL_MARKERS = (
    (b"kernel panic", "kernel panic"),
    (b"oops:", "kernel oops"),
    (b"debian_rootfs_fail reason=", "Stage1 rootfs failure"),
    (b"debian_network_m5_fail reason=", "guest network failure"),
    (b"debian_browser_m6_fail reason=", "browser guest failure"),
    (b"debian_browser_m7_fail reason=", "Baidu page guest failure"),
    (b"fatal bus error", "GMAC fatal bus error"),
)


class GateFailure(RuntimeError):
    """One stable physical-gate contract failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class GateTarget(str, Enum):
    """The last guest milestone required by one physical gate run."""

    NETWORK = "network"
    DESKTOP = "desktop"
    BROWSER = "browser"

    def __str__(self) -> str:
        return self.value


class GateTermination(BaseException):
    """An operator termination request that must unwind the serial lifecycle."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"gate terminated by signal {signum}")


class TerminationSignals:
    """Turn the first HUP/TERM into cleanup and the second into a hard exit."""

    SIGNALS = (signal.SIGHUP, signal.SIGTERM)

    def __init__(self) -> None:
        self._handling = False
        self._previous: dict[int, signal.Handlers] = {}

    def _handle(self, signum: int, frame: object) -> None:
        del frame
        if self._handling:
            os._exit(128 + signum)
        self._handling = True
        raise GateTermination(signum)

    def __enter__(self) -> TerminationSignals:
        for signum in self.SIGNALS:
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)


@dataclass(frozen=True)
class GateConfig:
    """The bounded portions of one physical gate execution."""

    boot_timeout: float = 300.0
    drain_timeout: float = 2.0
    target: GateTarget = GateTarget.BROWSER

    def __post_init__(self) -> None:
        for name, value in (
            ("boot timeout", self.boot_timeout),
            ("drain timeout", self.drain_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if not isinstance(self.target, GateTarget):
            raise ValueError("target must be a GateTarget")


class GateOperations(Protocol):
    """Physical effects injected around the deterministic gate state machine."""

    def invalidate(self) -> None: ...
    def ensure_address_unused(self) -> None: ...
    def open_board(self) -> None: ...
    def boot(self) -> bytes: ...
    def read(self, deadline: float) -> bytes: ...
    def drain(self, deadline: float) -> bytes: ...
    def close_board(self) -> None: ...
    def publish(self, transcript: bytes, result: dict[str, object]) -> None: ...


def physical_bootargs(
    reboot_after: int | None = None,
    *,
    target: GateTarget = GateTarget.BROWSER,
) -> str:
    """Return one target-specific, volatile Asterinas command line."""

    if not isinstance(target, GateTarget):
        raise ValueError("target must be a GateTarget")

    restart = ""
    if reboot_after is not None:
        restart = f" asterinas.reboot_after={reboot_after}"
    if target is GateTarget.DESKTOP:
        bootargs = (
            "console=ttyS0 console=tty0 loglevel=info "
            f"init=/init {ROOTFS_WRITE_BOOTARG}{restart} "
            "systemd.setenv=ASTERINAS_DESKTOP_M4_CONSOLE=/dev/ttyS0 "
            "systemd.setenv=ASTERINAS_DESKTOP_BROWSER_ENABLED=0 "
            f"{DESKTOP_MASK_BOOTARGS} "
            "-- --root-init=systemd"
        )
        command_bytes = len(f'setenv bootargs "{bootargs}"'.encode())
        if command_bytes >= MAX_UBOOT_COMMAND_BYTES:
            raise GateFailure("U-Boot bootargs command exceeds 1023 bytes")
        return bootargs

    evidence_variables = (
        ("ASTERINAS_DESKTOP_M5_CONSOLE",)
        if target is GateTarget.NETWORK
        else SERIAL_EVIDENCE_VARIABLES
    )
    evidence = " ".join(
        f"systemd.setenv={name}=/dev/ttyS0" for name in evidence_variables
    )
    bootargs = (
        "console=ttyS0 console=tty0 loglevel=info "
        f"init=/init {ROOTFS_WRITE_BOOTARG} {NETWORK_BOOTARG} "
        f"{NEIGHBOR_BOOTARGS}{restart} {evidence} {DESKTOP_PROXY_BOOTARGS} "
        f"{DESKTOP_FIXTURE_BOOTARGS} "
        "-- --root-init=systemd"
    )
    command_bytes = len(f'setenv bootargs "{bootargs}"'.encode())
    if command_bytes >= MAX_UBOOT_COMMAND_BYTES:
        raise GateFailure("U-Boot bootargs command exceeds 1023 bytes")
    return bootargs


def bounded_reboot_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("reboot delay must be an integer") from error
    if seconds < 30 or seconds > 3600:
        raise argparse.ArgumentTypeError("reboot delay must be between 30 and 3600")
    return seconds


def classify_physical_transcript(transcript: bytes) -> GateResult:
    """Classify all selected-GMAC, desktop, and network evidence in order."""

    if not isinstance(transcript, bytes):
        return GateResult(False, "physical transcript must be bytes", None)
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        return GateResult(False, "physical transcript exceeds 8 MiB", None)
    fatal_reason = _fatal_transcript_reason(transcript)
    if fatal_reason is not None:
        return GateResult(False, fatal_reason, None)

    positions: list[int] = []
    for marker in PHYSICAL_MILESTONES:
        count = transcript.count(marker)
        if count == 0:
            return GateResult(False, "missing physical milestone", None)
        if count != 1:
            return GateResult(False, "duplicate physical milestone", None)
        positions.append(transcript.find(marker))

    javascript_matches = tuple(_BROWSER_JAVASCRIPT_RE.finditer(transcript))
    if len(javascript_matches) != 1:
        return GateResult(False, "missing or duplicate JavaScript evidence", None)
    ready_matches = tuple(_BROWSER_READY_RE.finditer(transcript))
    if len(ready_matches) != 1:
        return GateResult(False, "missing or duplicate browser ready evidence", None)
    javascript = javascript_matches[0]
    ready = ready_matches[0]
    if javascript.group(1) != ready.group(1):
        return GateResult(False, "missing or mismatched browser ready evidence", None)
    positions.extend((javascript.start(), ready.start()))
    for marker in PHYSICAL_M7_MILESTONES:
        count = transcript.count(marker)
        if count == 0:
            return GateResult(False, "missing physical Baidu page milestone", None)
        if count != 1:
            return GateResult(False, "duplicate physical Baidu page milestone", None)
        positions.append(transcript.find(marker))
    if positions != sorted(positions):
        return GateResult(False, "physical milestones out of order", None)
    return GateResult(True, "pass", None)


def classify_physical_network_transcript(transcript: bytes) -> GateResult:
    """Classify selected-GMAC and Megrez M5 evidence without desktop coupling."""

    if not isinstance(transcript, bytes):
        return GateResult(False, "physical transcript must be bytes", None)
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        return GateResult(False, "physical transcript exceeds 8 MiB", None)
    fatal_reason = _fatal_transcript_reason(transcript)
    if fatal_reason is not None:
        return GateResult(False, fatal_reason, None)

    positions: list[int] = []
    for marker in PHYSICAL_NETWORK_MILESTONES:
        count = transcript.count(marker)
        if count == 0:
            return GateResult(False, "missing physical network milestone", None)
        if count != 1:
            return GateResult(False, "duplicate physical network milestone", None)
        positions.append(transcript.find(marker))
    if positions != sorted(positions):
        return GateResult(False, "physical network milestones out of order", None)
    return GateResult(True, "pass", None)


def classify_physical_desktop_transcript(transcript: bytes) -> GateResult:
    """Classify the browser-free desktop milestones in strict order."""

    if not isinstance(transcript, bytes):
        return GateResult(False, "physical transcript must be bytes", None)
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        return GateResult(False, "physical transcript exceeds 8 MiB", None)
    fatal_reason = _fatal_transcript_reason(transcript)
    if fatal_reason is not None:
        return GateResult(False, fatal_reason, None)

    positions: list[int] = []
    for marker in PHYSICAL_DESKTOP_MILESTONES:
        count = transcript.count(marker)
        if count == 0:
            return GateResult(False, "missing physical desktop milestone", None)
        if count != 1:
            return GateResult(False, "duplicate physical desktop milestone", None)
        positions.append(transcript.find(marker))
    if positions != sorted(positions):
        return GateResult(False, "physical desktop milestones out of order", None)
    return GateResult(True, "pass", None)


def _fatal_transcript_reason(transcript: bytes) -> str | None:
    lowered = transcript.lower()
    for marker, reason in _FATAL_MARKERS:
        if marker in lowered:
            return f"fatal transcript marker: {reason}"
    return None


def _browser_javascript_status(transcript: bytes) -> str:
    match = _BROWSER_JAVASCRIPT_RE.search(transcript)
    if match is None:
        raise GateFailure("missing JavaScript evidence")
    return match.group(1).decode("ascii")


def check_address_unused(
    interface: str,
    address: str = BOARD_ADDRESS,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """Reject a duplicate board address before the serial device is opened."""

    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", interface) is None:
        raise GateFailure("invalid host interface")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise GateFailure("invalid board IPv4 address") from error
    if parsed.version != 4:
        raise GateFailure("invalid board IPv4 address")
    argv = ("arping", "-D", "-c", "2", "-w", "3", "-I", interface, address)
    completed = run(argv, capture_output=True, check=False)
    if completed.returncode == 0:
        return
    if completed.returncode == 1:
        raise GateFailure("board IPv4 address is already in use")
    raise GateFailure(
        f"duplicate-address probe failed with status {completed.returncode}"
    )


def _append_transcript(transcript: bytearray, chunk: bytes) -> None:
    if not isinstance(chunk, bytes):
        raise GateFailure("serial reader returned non-bytes data")
    if len(transcript) + len(chunk) > MAX_TRANSCRIPT_BYTES:
        raise GateFailure("physical transcript exceeds 8 MiB")
    transcript.extend(chunk)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, GateFailure):
        return error.reason
    if isinstance(error, TimeoutError):
        return "physical boot deadline expired"
    message = str(error).strip()
    return message or error.__class__.__name__


def run_gate(config: GateConfig, operations: GateOperations) -> dict[str, object]:
    """Run one result-invalidating, bounded physical Ethernet transaction."""

    transcript = bytearray()
    opened = False
    drained = False
    cleanup_termination: GateTermination | None = None
    result: dict[str, object]
    target = config.target.value
    if config.target is GateTarget.NETWORK:
        ready_markers = PHYSICAL_NETWORK_READY_MARKERS
        classifier = classify_physical_network_transcript
    elif config.target is GateTarget.DESKTOP:
        ready_markers = PHYSICAL_DESKTOP_READY_MARKERS
        classifier = classify_physical_desktop_transcript
    else:
        ready_markers = PHYSICAL_READY_MARKERS
        classifier = classify_physical_transcript
    operations.invalidate()
    try:
        if config.target is not GateTarget.DESKTOP:
            operations.ensure_address_unused()
        operations.open_board()
        opened = True
        _append_transcript(transcript, operations.boot())
        if fatal_reason := _fatal_transcript_reason(bytes(transcript)):
            raise GateFailure(fatal_reason)
        deadline = time.monotonic() + config.boot_timeout
        while not any(marker in transcript for marker in ready_markers):
            if time.monotonic() >= deadline:
                raise TimeoutError
            chunk = operations.read(deadline)
            if not chunk:
                continue
            _append_transcript(transcript, chunk)
            if fatal_reason := _fatal_transcript_reason(bytes(transcript)):
                raise GateFailure(fatal_reason)
        classification = classifier(bytes(transcript))
        if not classification.passed:
            raise GateFailure(classification.reason)
        _append_transcript(
            transcript,
            operations.drain(time.monotonic() + config.drain_timeout),
        )
        drained = True
        classification = classifier(bytes(transcript))
        if not classification.passed:
            raise GateFailure(classification.reason)
        result = {
            "passed": True,
            "reason": "pass",
            "target": target,
            "board_address": BOARD_ADDRESS,
            "host_address": HOST_ADDRESS,
        }
        if config.target is GateTarget.BROWSER:
            result["javascript_status"] = _browser_javascript_status(bytes(transcript))
    except Exception as error:
        result = {
            "passed": False,
            "reason": _failure_reason(error),
            "target": target,
        }
    finally:
        if opened:
            if not drained:
                try:
                    _append_transcript(
                        transcript,
                        operations.drain(time.monotonic() + config.drain_timeout),
                    )
                except GateTermination as error:
                    cleanup_termination = error
                except Exception:
                    pass
            try:
                operations.close_board()
            except GateTermination as error:
                cleanup_termination = error
            except Exception as error:
                result = {
                    "passed": False,
                    "reason": _failure_reason(error),
                    "target": target,
                }
        if cleanup_termination is not None:
            raise cleanup_termination
    operations.publish(bytes(transcript), result)
    return result


class PhysicalGateOperations:
    """Concrete serial, duplicate-address, and pinned-output adapter."""

    def __init__(
        self,
        arguments: argparse.Namespace,
        *,
        fixture: FixtureServer | None = None,
    ) -> None:
        self.arguments = arguments
        self.output = PinnedOutputDirectory(arguments.output_directory)
        self.session: BoardSession | None = None
        self.fixture = fixture or FixtureServer(
            FixtureConfig(
                HOST_ADDRESS,
                FIXTURE_PORT,
                allowed_peer=BOARD_ADDRESS,
            )
        )

    def _uses_network(self) -> bool:
        return (
            getattr(self.arguments, "target", GateTarget.BROWSER)
            is not GateTarget.DESKTOP
        )

    def __enter__(self) -> PhysicalGateOperations:
        try:
            if self._uses_network():
                self.fixture.start()
            return self
        except BaseException:
            self.output.close()
            raise

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        try:
            self.close_board()
        finally:
            try:
                if self._uses_network():
                    self.fixture.close()
            finally:
                self.output.close()

    def invalidate(self) -> None:
        self.output.invalidate(
            "megrez-gmac.serial.log",
            "network-fixture.json",
            "result.json",
        )

    def ensure_address_unused(self) -> None:
        check_address_unused(self.arguments.host_interface)

    def open_board(self) -> None:
        self.session = BoardSession(
            self.arguments.device,
            os.devnull,
            confirm=False,
        )

    def _session(self) -> BoardSession:
        if self.session is None:
            raise GateFailure("serial session is not open")
        return self.session

    def boot(self) -> bytes:
        session = self._session()
        session.send("")
        prompt = session.wait_for_uboot_prompt(timeout=self.arguments.uboot_timeout)
        boot_arguments = argparse.Namespace(
            booti=self.arguments.booti,
            dtb=self.arguments.dtb,
            initrd=self.arguments.initrd,
            expected_crc32=self.arguments.expected_crc32,
            firmware_framebuffer=True,
            bootargs=physical_bootargs(
                self.arguments.reboot_after,
                target=self.arguments.target,
            ),
            load_transport=self.arguments.load_transport,
            tftp_board_address=self.arguments.tftp_board_address,
            tftp_server_address=self.arguments.tftp_server_address,
            tftp_netmask=self.arguments.tftp_netmask,
            ymodem_directory=self.arguments.ymodem_directory,
            booti_compressed_crc32=self.arguments.booti_compressed_crc32,
            booti_uncompressed_size=self.arguments.booti_uncompressed_size,
        )
        entered = boot_loaded_artifacts(session, boot_arguments)
        return (prompt + entered).encode(errors="replace")

    def read(self, deadline: float) -> bytes:
        remaining = max(0.0, deadline - time.monotonic())
        return read_available(self._session().fd, min(1.0, remaining)).encode(
            errors="replace"
        )

    def drain(self, deadline: float) -> bytes:
        remaining = max(0.0, deadline - time.monotonic())
        return read_available(self._session().fd, remaining).encode(errors="replace")

    def close_board(self) -> None:
        if self.session is None:
            return
        session = self.session
        self.session = None
        session.log.close()
        os.close(session.fd)

    def publish(self, transcript: bytes, result: dict[str, object]) -> None:
        self.output.atomic_write("megrez-gmac.serial.log", transcript)
        if self._uses_network():
            summary = self.fixture.summary()
            result["network_fixture"] = summary
            if result.get("passed") is True and not is_successful_summary(
                summary, expected_requests=FIXTURE_REQUESTS
            ):
                result["passed"] = False
                result["reason"] = "network fixture evidence mismatch"
            fixture_payload = (
                json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            self.output.atomic_write("network-fixture.json", fixture_payload)
        payload = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
        self.output.atomic_write("result.json", payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--booti", required=True, type=safe_artifact_name)
    parser.add_argument("--initrd", required=True, type=safe_artifact_name)
    parser.add_argument("--dtb", required=True, type=safe_artifact_name)
    parser.add_argument("--expected-crc32", required=True, type=parse_expected_crc32)
    parser.add_argument("--host-interface", required=True)
    parser.add_argument(
        "--load-transport", choices=("mmc", "tftp", "ymodem"), default="mmc"
    )
    parser.add_argument("--tftp-board-address", type=safe_ipv4, default=BOARD_ADDRESS)
    parser.add_argument("--tftp-server-address", type=safe_ipv4, default=HOST_ADDRESS)
    parser.add_argument(
        "--tftp-netmask", type=safe_ipv4_netmask, default="255.255.248.0"
    )
    parser.add_argument("--ymodem-directory", type=Path)
    parser.add_argument("--booti-compressed-crc32", type=crc32_value)
    parser.add_argument("--booti-uncompressed-size", type=positive_size)
    parser.add_argument("--uboot-timeout", type=positive_finite_seconds, default=60.0)
    parser.add_argument("--reboot-after", type=bounded_reboot_seconds)
    parser.add_argument(
        "--target",
        choices=tuple(GateTarget),
        default=GateTarget.BROWSER,
        type=GateTarget,
    )
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--boot-timeout", type=positive_finite_seconds, default=300.0)
    parser.add_argument("--drain-timeout", type=positive_finite_seconds, default=2.0)
    return parser


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _parser()
    values = parser.parse_args(arguments)
    ymodem_contract = (
        values.ymodem_directory,
        values.booti_compressed_crc32,
        values.booti_uncompressed_size,
    )
    if values.load_transport == "ymodem" and any(
        value is None for value in ymodem_contract
    ):
        parser.error(
            "--load-transport ymodem requires --ymodem-directory, "
            "--booti-compressed-crc32, and --booti-uncompressed-size"
        )
    if values.load_transport != "ymodem" and any(
        value is not None for value in ymodem_contract
    ):
        parser.error("YMODEM options require --load-transport ymodem")
    return values


def main(arguments: Sequence[str] | None = None) -> int:
    values = _parse_args(arguments)
    config = GateConfig(
        boot_timeout=values.boot_timeout,
        drain_timeout=values.drain_timeout,
        target=values.target,
    )
    try:
        with TerminationSignals():
            with PhysicalGateOperations(values) as operations:
                result = run_gate(config, operations)
        return 0 if result["passed"] else 1
    except GateTermination as error:
        print(f"megrez-gmac-gate: terminated by signal {error.signum}", file=sys.stderr)
        return 128 + error.signum
    except BaseException as error:
        print(f"megrez-gmac-gate: {_failure_reason(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
