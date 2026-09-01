# SPDX-License-Identifier: MPL-2.0

"""Cache-aware physical transport for one frozen Megrez debug plan."""

from __future__ import annotations

import errno
import fcntl
import gzip
import io
import json
import math
import os
import re
import signal
import stat
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, TextIO

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_board_session import (
    CRC_RESULT_PATTERN,
    MEGREZ_FRAMEBUFFER,
    MEGREZ_USB_HOST_COMMAND,
    UBOOT_ERROR_PATTERN,
    BoardSession,
    open_serial,
    read_available,
)
from tools.riscv.megrez_debug_contract import (
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_xmodem import (
    INITIAL_BAUD,
    read_artifact,
    transfer_payload_fd,
)

BOARD_ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
Command = Callable[[str, float], str]
TransferPayload = Callable[[int, bytes, int], object]
OpenDevice = Callable[[str], int]
LockDevice = Callable[[int], None]
CloseDevice = Callable[[int], None]
SessionFactory = Callable[..., BoardSession]
ProbeTraceProvider = Callable[[str], bytes]
MAX_BOARD_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_UBOOT_COMMAND_BYTES = 512
_UBOOT_BOOTARGS_CHUNK_BYTES = 384
FATAL_MARKERS = (
    "Uncaught panic",
    "unexpected exception",
    "Kernel panic",
    "Oops:",
    "MEGREZ_SDHCI_READ_FAIL",
)
PROMPT_PATTERN = re.compile(r"(?:^|[\r\n])=> ")
UBOOT_AUTOBOOT_PATTERN = re.compile(
    r"(?:^|[\r\n])Hit any key to stop autoboot:\s*[0-9]+"
)
UBOOT_BANNER_PATTERN = re.compile(r"(?:^|[\r\n])U-Boot [0-9]{4}\.[0-9]{2}")
PROBE_FAILURE_PATTERN = re.compile(
    r"ASTERINAS_GMAC_TCP_PROBE_FAIL "
    r"reason=(?P<reason>[a-z0-9-]+) "
    r"errno=[0-9]+ attempts=[0-9]+ "
    r"current_bytes=[0-9]+ completed_bytes=[0-9]+(?:\r?\n|$)"
)
_POINTER_MISSING_DIAGNOSTIC = "DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=pointer-device"
_M4_TIMEOUT_FAILURE = "DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout"
_POINTER_MISSING_REASON = "browser-pass-input-missing:pointer-device"
KERNEL_COMPRESSED_ADDRESS = 0x90000000
EIC7700_WATCHDOG_BASE = 0x50800000
EIC7700_SYSCFG_WATCHDOG_CLOCK = 0x51828200
EIC7700_SYSCFG_WATCHDOG_RESET = 0x51828444
EIC7700_WATCHDOG0_CLOCK_BIT = 1 << 28
EIC7700_WATCHDOG0_RESET_N_BIT = 1 << 0
DW_WATCHDOG_COMPONENT_TYPE = 0x44570120
DW_WATCHDOG_MAX_TIMEOUT = 0x0F
MAX_BOARD_TIMEOUT = 900.0
DW_WATCHDOG_RECOVERY_CONTROL = 0x1F
_UBOOT_MEMORY_LINE = re.compile(
    r"(?m)^(?P<address>[0-9a-fA-F]{8,16}):"
    r"(?P<values>(?: [0-9a-fA-F]{8})+)"
    r"(?:[ \t]{2,}[^\r\n]*)?\r*$"
)


class BoardTransportError(RuntimeError):
    """One cache validation or XMODEM transport failure."""


def _uboot_words(output: str, address: int, count: int) -> tuple[int, ...]:
    for match in _UBOOT_MEMORY_LINE.finditer(output):
        if int(match.group("address"), 16) != address:
            continue
        values = tuple(int(value, 16) for value in match.group("values").split())
        if len(values) == count:
            return values
    raise BoardRunFailure("hardware-watchdog-readback-invalid")


@dataclass(frozen=True)
class TransportOutcome:
    """How one exact artifact became resident in board RAM."""

    artifact: str
    status: str

    def __post_init__(self) -> None:
        if self.artifact not in BOARD_ARTIFACT_NAMES:
            raise ValueError("unknown board artifact")
        if self.status not in ("cache-hit", "transferred", "transferred-compressed"):
            raise ValueError("unknown transport outcome")


def _default_transfer_payload(fd: int, payload: bytes, address: int) -> object:
    return transfer_payload_fd(fd, payload, address, current_baud=INITIAL_BAUD)


class BoardTransport:
    """Verify and populate RAM through one caller-owned serial descriptor."""

    def __init__(
        self,
        *,
        fd: int,
        command: Command,
        transfer_payload: TransferPayload = _default_transfer_payload,
    ) -> None:
        if type(fd) is not int or fd < 0:
            raise ValueError("serial descriptor must be non-negative")
        self._fd = fd
        self._command = command
        self._transfer_payload = transfer_payload

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise BoardTransportError("transport-timeout-invalid")

    def _resident_crc(self, identity: ArtifactIdentity, timeout: float) -> str:
        command = f"crc32 0x{identity.load_address:x} 0x{identity.size:x}"
        output = self._command(command, timeout)
        if UBOOT_ERROR_PATTERN.search(output):
            raise BoardTransportError(f"transport-crc-command: {identity.name}")
        match = CRC_RESULT_PATTERN.search(output)
        if match is None or int(match.group(2), 16) != identity.load_address:
            raise BoardTransportError(f"transport-crc-result: {identity.name}")
        return match.group(3).lower()

    def ensure(self, identity: ArtifactIdentity, *, timeout: float) -> TransportOutcome:
        """Reuse matching RAM or transfer once and verify the resulting bytes."""

        self._validate_timeout(timeout)
        if identity.name not in BOARD_ARTIFACT_NAMES:
            raise BoardTransportError(f"transport-artifact-invalid: {identity.name}")
        if self._resident_crc(identity, timeout) == identity.crc32:
            return TransportOutcome(identity.name, "cache-hit")

        try:
            payload = read_artifact(Path(identity.path))
            if identity.name == "kernel":
                compressed = gzip.compress(payload, compresslevel=1, mtime=0)
                self._transfer_payload(
                    self._fd,
                    compressed,
                    KERNEL_COMPRESSED_ADDRESS,
                )
                self._command(
                    f"unzip 0x{KERNEL_COMPRESSED_ADDRESS:x} "
                    f"0x{identity.load_address:x} 0x{identity.size:x}",
                    timeout,
                )
                status = "transferred-compressed"
            else:
                self._transfer_payload(
                    self._fd,
                    payload,
                    identity.load_address,
                )
                status = "transferred"
        except (OSError, RuntimeError) as error:
            raise BoardTransportError(
                f"transport-xmodem: {identity.name}: {error}"
            ) from error
        if self._resident_crc(identity, timeout) != identity.crc32:
            raise BoardTransportError(f"transport-post-transfer-crc: {identity.name}")
        return TransportOutcome(identity.name, status)


def ensure_board_artifacts(
    plan: DebugPlan, transport: BoardTransport, *, timeout: float
) -> tuple[TransportOutcome, ...]:
    """Ensure only the three artifacts consumed by physical `booti`."""

    plan.validate()
    identities = {identity.name: identity for identity in plan.artifacts}
    return tuple(
        transport.ensure(identities[name], timeout=timeout)
        for name in BOARD_ARTIFACT_NAMES
    )


class BoardRunFailure(RuntimeError):
    """One stable state-machine failure reason."""


class BoardTermination(RuntimeError):
    """A first termination signal deferred through close and publication."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"board termination signal {signum}")
        self.signum = signum


def _uboot_bootargs_commands(bootargs: str) -> tuple[str, ...]:
    """Stage exact boot arguments without exceeding U-Boot's line buffer."""

    tokens = bootargs.split()
    if not tokens or " ".join(tokens) != bootargs:
        raise BoardRunFailure("uboot-bootargs-not-canonical")
    chunks: list[str] = []
    current = ""
    for token in tokens:
        candidate = f"{current} {token}" if current else token
        if len(candidate.encode()) <= _UBOOT_BOOTARGS_CHUNK_BYTES:
            current = candidate
            continue
        if not current or len(token.encode()) > _UBOOT_BOOTARGS_CHUNK_BYTES:
            raise BoardRunFailure("uboot-bootargs-token-too-long")
        chunks.append(current)
        current = token
    chunks.append(current)

    names = tuple(f"asterinas_bootargs_{index}" for index in range(len(chunks)))
    commands = (
        *(f'setenv {name} "{chunk}"' for name, chunk in zip(names, chunks)),
        f'setenv bootargs "{" ".join(f"${{{name}}}" for name in names)}"',
        *(f"setenv {name}" for name in names),
    )
    if any(len(command.encode()) > MAX_UBOOT_COMMAND_BYTES for command in commands):
        raise BoardRunFailure("uboot-bootargs-command-too-long")
    return commands


@dataclass(frozen=True)
class BoardRunConfig:
    """Bounded policy for one physical boot attempt."""

    timeout: float = 300.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= MAX_BOARD_TIMEOUT
        ):
            raise ValueError(
                "board timeout must be finite, positive, and at most "
                f"{MAX_BOARD_TIMEOUT:g}"
            )


class BoardOperations(Protocol):
    """Injected physical operations owned by the production board adapter."""

    def invalidate(self) -> None: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]: ...

    def prepare_boot(self, plan: DebugPlan, timeout: float) -> None: ...

    def booti(self, plan: DebugPlan, timeout: float) -> None: ...

    def read_chunk(self, timeout: float) -> str: ...

    def stop_recovery_autoboot(self, timeout: float) -> None: ...

    def close(self) -> None: ...

    def evidence_names(self) -> tuple[str, ...]: ...

    def publish(
        self,
        result: StageResult,
        transcript: str,
        outcomes: tuple[str, ...],
    ) -> None: ...


def _lock_serial(fd: int) -> None:
    """Hold a cooperative lock and the kernel's exclusive TTY mode."""

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.ioctl(fd, termios.TIOCEXCL)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTTY):
            raise


def _safe_board_output(path: Path, repository_root: Path) -> Path:
    repository = repository_root.absolute()
    allowed = repository / "target" / "megrez-debug"
    candidate = path.absolute()
    try:
        candidate.relative_to(allowed)
    except ValueError as error:
        raise BoardRunFailure("board-output-outside-target") from error

    current = repository
    for component in candidate.relative_to(repository).parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BoardRunFailure("board-output-unsafe")
    candidate.mkdir(parents=True, mode=0o755, exist_ok=True)
    return candidate


class RealBoardOperations:
    """One descriptor-owned physical-board adapter for ``run_board``."""

    def __init__(
        self,
        plan: DebugPlan,
        device: str,
        output_directory: Path,
        *,
        repository_root: Path | None = None,
        open_device: OpenDevice = open_serial,
        lock_device: LockDevice = _lock_serial,
        close_device: CloseDevice = os.close,
        session_factory: SessionFactory = BoardSession.from_fd,
        probe_trace_provider: ProbeTraceProvider | None = None,
        hardware_watchdog: bool = False,
    ) -> None:
        self._plan = plan
        self._device = device
        self._output_path = output_directory
        self._repository = (
            repository_root.absolute()
            if repository_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self._open_device = open_device
        self._lock_device = lock_device
        self._close_device = close_device
        self._session_factory = session_factory
        self._probe_trace_provider = probe_trace_provider
        self._hardware_watchdog = hardware_watchdog
        self._output: PinnedOutputDirectory | None = None
        self._fd: int | None = None
        self._session: BoardSession | None = None
        self._log: TextIO = io.StringIO()
        self.last_transcript = ""
        self.last_outcomes: tuple[str, ...] = ()

    @property
    def can_publish(self) -> bool:
        return self._output is not None

    def invalidate(self) -> None:
        output = _safe_board_output(self._output_path, self._repository)
        self._output = PinnedOutputDirectory(output)
        self._output.invalidate("result.json", *self.evidence_names())

    def evidence_names(self) -> tuple[str, ...]:
        names = ("serial.log", "transport.json")
        if self._probe_trace_provider is not None:
            names += ("probe-tcp-info.json",)
        return names

    def open(self, timeout: float) -> None:
        fd = self._open_device(self._device)
        try:
            self._lock_device(fd)
            session = self._session_factory(
                fd,
                None,
                confirm=False,
                final_marker=self._plan.markers[-1],
                log_stream=self._log,
            )
            session.send("")
            session.wait_for_uboot_prompt(timeout)
        except BaseException:
            self._close_device(fd)
            raise
        self._fd = fd
        self._session = session

    def _require_session(self) -> BoardSession:
        if self._session is None or self._fd is None:
            raise BoardRunFailure("transport-not-open")
        return self._session

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]:
        session = self._require_session()
        deadline = time.monotonic() + timeout
        transport = BoardTransport(
            fd=self._fd,
            command=lambda command, budget: session.command(command, timeout=budget),
        )
        identities = {identity.name: identity for identity in plan.artifacts}
        outcomes: list[str] = []
        for name in BOARD_ARTIFACT_NAMES:
            outcome = transport.ensure(
                identities[name],
                timeout=_remaining(deadline, time.monotonic, phase="transport"),
            )
            outcomes.append(f"{outcome.artifact}:{outcome.status}")
        return tuple(outcomes)

    def prepare_boot(self, plan: DebugPlan, timeout: float) -> None:
        session = self._require_session()
        deadline = time.monotonic() + timeout
        initramfs = next(item for item in plan.artifacts if item.name == "initramfs")
        commands = (
            "mmc dev 1",
            "mmc rescan",
            "fdt addr 0xf0000000",
            "fdt resize 0x1000",
            *MEGREZ_FRAMEBUFFER.commands(),
            f"setenv initrd_size 0x{initramfs.size:x}",
            *_uboot_bootargs_commands(plan.bootargs),
            MEGREZ_USB_HOST_COMMAND,
        )
        for command in commands:
            session.command(
                command,
                timeout=_remaining(deadline, time.monotonic, phase="uboot-prepare"),
            )
        if self._hardware_watchdog:
            self._arm_hardware_watchdog(session, deadline)

    @staticmethod
    def _arm_hardware_watchdog(session: BoardSession, deadline: float) -> None:
        def command(text: str) -> str:
            return session.command(
                text,
                timeout=_remaining(deadline, time.monotonic, phase="hardware-watchdog"),
            )

        clock = _uboot_words(
            command(f"md.l 0x{EIC7700_SYSCFG_WATCHDOG_CLOCK:x} 1"),
            EIC7700_SYSCFG_WATCHDOG_CLOCK,
            1,
        )[0]
        reset = _uboot_words(
            command(f"md.l 0x{EIC7700_SYSCFG_WATCHDOG_RESET:x} 1"),
            EIC7700_SYSCFG_WATCHDOG_RESET,
            1,
        )[0]
        if clock & EIC7700_WATCHDOG0_CLOCK_BIT == 0:
            command(
                f"mw.l 0x{EIC7700_SYSCFG_WATCHDOG_CLOCK:x} "
                f"0x{clock | EIC7700_WATCHDOG0_CLOCK_BIT:x}"
            )
        if reset & EIC7700_WATCHDOG0_RESET_N_BIT == 0:
            command(
                f"mw.l 0x{EIC7700_SYSCFG_WATCHDOG_RESET:x} "
                f"0x{reset | EIC7700_WATCHDOG0_RESET_N_BIT:x}"
            )
        clock = _uboot_words(
            command(f"md.l 0x{EIC7700_SYSCFG_WATCHDOG_CLOCK:x} 1"),
            EIC7700_SYSCFG_WATCHDOG_CLOCK,
            1,
        )[0]
        reset = _uboot_words(
            command(f"md.l 0x{EIC7700_SYSCFG_WATCHDOG_RESET:x} 1"),
            EIC7700_SYSCFG_WATCHDOG_RESET,
            1,
        )[0]
        if (
            clock & EIC7700_WATCHDOG0_CLOCK_BIT == 0
            or reset & EIC7700_WATCHDOG0_RESET_N_BIT == 0
        ):
            raise BoardRunFailure("hardware-watchdog-prerequisite-not-ready")

        component = command(f"md.l 0x{EIC7700_WATCHDOG_BASE + 0xFC:x} 1")
        if _uboot_words(component, EIC7700_WATCHDOG_BASE + 0xFC, 1) != (
            DW_WATCHDOG_COMPONENT_TYPE,
        ):
            raise BoardRunFailure("hardware-watchdog-type-mismatch")
        command(
            f"mw.l 0x{EIC7700_WATCHDOG_BASE + 0x04:x} 0x{DW_WATCHDOG_MAX_TIMEOUT:x}"
        )
        command(f"mw.l 0x{EIC7700_WATCHDOG_BASE + 0x0C:x} 0x76")
        command(f"mw.l 0x{EIC7700_WATCHDOG_BASE:x} 0x{DW_WATCHDOG_RECOVERY_CONTROL:x}")
        control, timeout = _uboot_words(
            command(f"md.l 0x{EIC7700_WATCHDOG_BASE:x} 2"),
            EIC7700_WATCHDOG_BASE,
            2,
        )
        if (
            control & DW_WATCHDOG_RECOVERY_CONTROL != DW_WATCHDOG_RECOVERY_CONTROL
            or timeout & 0x0F != DW_WATCHDOG_MAX_TIMEOUT
        ):
            raise BoardRunFailure("hardware-watchdog-not-armed")

    def booti(self, plan: DebugPlan, timeout: float) -> None:
        del timeout
        session = self._require_session()
        identities = {identity.name: identity for identity in plan.artifacts}
        kernel = identities["kernel"]
        initramfs = identities["initramfs"]
        dtb = identities["megrez_dtb"]
        session.send(
            f"booti 0x{kernel.load_address:x} "
            f"0x{initramfs.load_address:x}:0x{initramfs.size:x} "
            f"0x{dtb.load_address:x}"
        )

    def read_chunk(self, timeout: float) -> str:
        session = self._require_session()
        chunk = read_available(self._fd, min(timeout, 1.0))
        if chunk:
            session._log(chunk)
        return chunk

    def stop_recovery_autoboot(self, timeout: float) -> None:
        del timeout
        self._require_session().send("")

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        self._session = None
        self._close_device(fd)

    def publish(
        self,
        result: StageResult,
        transcript: str,
        outcomes: tuple[str, ...],
    ) -> None:
        if self._output is None:
            raise BoardRunFailure("board-output-not-pinned")
        self.last_transcript = transcript
        self.last_outcomes = outcomes
        transport = {
            "schema_version": 1,
            "plan_sha256": self._plan.plan_sha256,
            "outcomes": list(outcomes),
        }
        self._output.atomic_write(
            "serial.log", self._log.getvalue().encode(), mode=0o644
        )
        self._output.atomic_write(
            "transport.json",
            json.dumps(transport, sort_keys=True, separators=(",", ":")).encode()
            + b"\n",
            mode=0o644,
        )
        if self._probe_trace_provider is not None:
            trace = self._probe_trace_provider(self._plan.plan_sha256)
            try:
                decoded = json.loads(trace.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BoardRunFailure("probe-trace-invalid") from error
            if (
                not isinstance(decoded, dict)
                or decoded.get("schema_version") != 1
                or decoded.get("plan_sha256") != self._plan.plan_sha256
            ):
                raise BoardRunFailure("probe-trace-plan-mismatch")
            self._output.atomic_write("probe-tcp-info.json", trace, mode=0o644)
        if result.evidence != self.evidence_names():
            raise BoardRunFailure("board-evidence-mismatch")
        self._output.atomic_write("result.json", result.canonical_bytes(), mode=0o644)

    def finish(self) -> None:
        self.close()
        self._log.close()
        if self._output is not None:
            self._output.close()
            self._output = None


class _BoardSignalState:
    """Convert the first operator signal into orderly board cleanup."""

    SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)

    def __init__(self) -> None:
        self._handling = False
        self._previous: dict[int, signal.Handlers] = {}

    def _handle(self, signum: int, frame: object) -> None:
        del frame
        if self._handling:
            os._exit(128 + signum)
        self._handling = True
        raise BoardTermination(signum)

    def __enter__(self) -> _BoardSignalState:
        for signum in self.SIGNALS:
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)


def run_physical_board(
    plan: DebugPlan,
    device: str,
    output_directory: Path,
    *,
    timeout: float = 300.0,
    probe_trace_provider: ProbeTraceProvider | None = None,
    hardware_watchdog: bool = False,
) -> StageResult:
    """Run one board attempt without reset or persistent U-Boot writes."""

    operations = RealBoardOperations(
        plan,
        device,
        output_directory,
        probe_trace_provider=probe_trace_provider,
        hardware_watchdog=hardware_watchdog,
    )
    try:
        try:
            with _BoardSignalState():
                return run_board(plan, BoardRunConfig(timeout), operations)
        except BoardTermination as error:
            if operations.can_publish:
                operations.publish(
                    _stage_result(
                        plan,
                        passed=False,
                        reason=f"board-terminated-{error.signum}",
                        evidence=operations.evidence_names(),
                    ),
                    operations.last_transcript,
                    operations.last_outcomes,
                )
            raise
    finally:
        operations.finish()


class _MarkerTracker:
    def __init__(
        self,
        markers: tuple[str, ...],
        *,
        allow_pointer_degradation: bool = False,
    ) -> None:
        self._markers = markers
        self._index = 0
        self._terminal: GuestTerminal | None = None
        self._autoboot_stop_pending = False
        self._autoboot_stop_sent = False
        self._pointer_diagnostic_seen = False
        self._degradation_reason: str | None = None
        if allow_pointer_degradation:
            self._m4_start = markers.index(DESKTOP_M4_MILESTONES[0])
            self._m4_end = self._m4_start + len(DESKTOP_M4_MILESTONES)
            if markers[self._m4_start : self._m4_end] != DESKTOP_M4_MILESTONES:
                raise ValueError("M4 milestones must be contiguous")
        else:
            self._m4_start = -1
            self._m4_end = -1
        self._tail = ""
        self._tail_limit = (
            max(
                *(len(marker) for marker in markers),
                *(len(marker) for marker in FATAL_MARKERS),
                512,
            )
            - 1
        )

    @property
    def complete(self) -> bool:
        return self._index == len(self._markers)

    @property
    def observed(self) -> int:
        return self._index

    @property
    def terminal(self) -> GuestTerminal | None:
        return self._terminal

    def take_autoboot_stop_request(self) -> bool:
        if not self._autoboot_stop_pending:
            return False
        self._autoboot_stop_pending = False
        self._autoboot_stop_sent = True
        return True

    def feed(self, chunk: str) -> bool:
        window = self._tail + chunk
        if any(marker in window for marker in FATAL_MARKERS):
            raise BoardRunFailure("kernel-fatal")

        cursor = 0
        while True:
            occurrences: list[tuple[int, str, int, int]] = [
                (position, "marker", index, position + len(marker))
                for index, marker in enumerate(self._markers)
                if (position := window.find(marker, cursor)) >= 0
            ]
            failure = PROBE_FAILURE_PATTERN.search(window, cursor)
            if failure is not None:
                occurrences.append((failure.start(), "failure", -1, failure.end()))
            if self._m4_start >= 0:
                for event, marker in (
                    ("pointer-diagnostic", _POINTER_MISSING_DIAGNOSTIC),
                    ("m4-timeout", _M4_TIMEOUT_FAILURE),
                ):
                    position = window.find(marker, cursor)
                    if position >= 0:
                        occurrences.append(
                            (position, event, -1, position + len(marker))
                        )
            banner = UBOOT_BANNER_PATTERN.search(window, cursor)
            if banner is not None:
                occurrences.append((banner.start(), "banner", -1, banner.end()))
            autoboot = UBOOT_AUTOBOOT_PATTERN.search(window, cursor)
            if autoboot is not None:
                occurrences.append((autoboot.start(), "autoboot", -1, autoboot.end()))
            if not occurrences:
                break
            _position, event, marker_index, event_end = min(occurrences)
            if event in ("banner", "autoboot"):
                if self._terminal is None and self._index > 0:
                    raise BoardRunFailure("guest-reboot-before-terminal")
                if (
                    self._terminal is not None
                    and event == "autoboot"
                    and not self._autoboot_stop_sent
                ):
                    self._autoboot_stop_pending = True
                cursor = event_end
                continue
            if self._terminal is not None:
                raise BoardRunFailure("guest-terminal-duplicate")
            if event == "pointer-diagnostic":
                if (
                    self._index != self._m4_start
                    or self._pointer_diagnostic_seen
                    or self._degradation_reason is not None
                ):
                    raise BoardRunFailure("guest-marker-order")
                self._pointer_diagnostic_seen = True
                cursor = event_end
                continue
            if event == "m4-timeout":
                if (
                    self._index != self._m4_start
                    or not self._pointer_diagnostic_seen
                    or self._degradation_reason is not None
                ):
                    raise BoardRunFailure("guest-marker-order")
                self._index = self._m4_end
                self._pointer_diagnostic_seen = False
                self._degradation_reason = _POINTER_MISSING_REASON
                cursor = event_end
                continue
            if event == "failure":
                if self._index == 0:
                    raise BoardRunFailure("guest-marker-order")
                assert failure is not None
                self._terminal = GuestTerminal(
                    passed=False,
                    reason=failure.group("reason"),
                )
                cursor = event_end
                continue
            if self._pointer_diagnostic_seen:
                raise BoardRunFailure("guest-marker-order")
            if marker_index != self._index:
                raise BoardRunFailure("guest-marker-order")
            self._index += 1
            cursor = event_end
            if self.complete:
                reason = self._degradation_reason
                self._terminal = GuestTerminal(
                    passed=reason is None,
                    reason=reason or "pass",
                )

        recovered = (
            self._terminal is not None
            and PROMPT_PATTERN.search(window, cursor) is not None
        )
        self._tail = window[cursor:][-self._tail_limit :]
        return recovered


@dataclass(frozen=True)
class GuestTerminal:
    """One current-attempt guest outcome observed before firmware recovery."""

    passed: bool
    reason: str


def _remaining(deadline: float, clock: Callable[[], float], *, phase: str) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise BoardRunFailure(f"{phase}-timeout")
    return remaining


def _stage_result(
    plan: DebugPlan,
    *,
    passed: bool,
    reason: str,
    evidence: tuple[str, ...],
) -> StageResult:
    result = StageResult(
        schema_version=1,
        stage="board",
        passed=passed,
        reason=reason,
        plan_sha256=plan.plan_sha256,
        evidence=evidence,
    )
    result.validate()
    return result


def _serial_timeout_reason(tracker: _MarkerTracker) -> str:
    if tracker.terminal is not None:
        return "recovery-not-observed"
    if tracker.observed == 0:
        return "kernel-timeout"
    return "guest-timeout"


def run_board(
    plan: DebugPlan,
    config: BoardRunConfig,
    operations: BoardOperations,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> StageResult:
    """Execute one current-attempt boot and wait for automatic recovery."""

    plan.validate()
    operations.invalidate()
    evidence = operations.evidence_names()
    deadline = clock() + config.timeout
    tracker = _MarkerTracker(
        plan.markers,
        allow_pointer_degradation=(plan.schema_version == 2),
    )
    transcript: list[str] = []
    transcript_bytes = 0
    outcomes: tuple[str, ...] = ()
    opened = False
    result: StageResult | None = None
    pending_termination: BoardTermination | None = None

    try:
        operations.open(_remaining(deadline, clock, phase="transport-open"))
        opened = True
        outcomes = operations.ensure_artifacts(
            plan, _remaining(deadline, clock, phase="transport")
        )
        operations.prepare_boot(
            plan, _remaining(deadline, clock, phase="uboot-prepare")
        )
        operations.booti(plan, _remaining(deadline, clock, phase="uboot-booti"))
        deadline = clock() + config.timeout

        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise BoardRunFailure(_serial_timeout_reason(tracker))
            try:
                chunk = operations.read_chunk(remaining)
            except TimeoutError:
                raise BoardRunFailure(_serial_timeout_reason(tracker)) from None
            transcript_bytes += len(chunk.encode("utf-8", errors="replace"))
            if transcript_bytes > MAX_BOARD_TRANSCRIPT_BYTES:
                raise BoardRunFailure("guest-transcript-limit")
            transcript.append(chunk)
            recovered = tracker.feed(chunk)
            if not recovered and tracker.take_autoboot_stop_request():
                operations.stop_recovery_autoboot(
                    _remaining(deadline, clock, phase="recovery-autoboot")
                )
            if recovered:
                terminal = tracker.terminal
                assert terminal is not None
                if terminal.passed:
                    result = _stage_result(
                        plan,
                        passed=True,
                        reason="board-pass",
                        evidence=evidence,
                    )
                else:
                    result = _stage_result(
                        plan,
                        passed=False,
                        reason=f"guest-failure-recovered:{terminal.reason}",
                        evidence=evidence,
                    )
                break
    except BoardTermination as error:
        pending_termination = error
        result = _stage_result(
            plan,
            passed=False,
            reason=f"board-terminated-{error.signum}",
            evidence=evidence,
        )
    except BoardTransportError as error:
        result = _stage_result(
            plan,
            passed=False,
            reason=str(error),
            evidence=evidence,
        )
    except BoardRunFailure as error:
        result = _stage_result(
            plan,
            passed=False,
            reason=str(error),
            evidence=evidence,
        )
    except (OSError, RuntimeError) as error:
        result = _stage_result(
            plan,
            passed=False,
            reason=f"uboot-runtime-{type(error).__name__}",
            evidence=evidence,
        )
    finally:
        if opened:
            try:
                operations.close()
            except BoardTermination as error:
                pending_termination = error
                result = _stage_result(
                    plan,
                    passed=False,
                    reason=f"board-terminated-{error.signum}",
                    evidence=evidence,
                )
            except (OSError, RuntimeError):
                if pending_termination is None:
                    result = _stage_result(
                        plan,
                        passed=False,
                        reason="transport-close-failed",
                        evidence=evidence,
                    )

    if result is None:
        result = _stage_result(
            plan,
            passed=False,
            reason="board-internal-error",
            evidence=evidence,
        )
    try:
        operations.publish(result, "".join(transcript), outcomes)
    except BoardTermination as error:
        pending_termination = error
        result = _stage_result(
            plan,
            passed=False,
            reason=f"board-terminated-{error.signum}",
            evidence=evidence,
        )
        try:
            operations.publish(result, "".join(transcript), outcomes)
        except (OSError, RuntimeError) as publication_error:
            raise BoardRunFailure("board-publication-failed") from publication_error
    except (OSError, RuntimeError) as error:
        raise BoardRunFailure("board-publication-failed") from error
    if pending_termination is not None:
        raise pending_termination
    return result
