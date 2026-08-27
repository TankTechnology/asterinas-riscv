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

from tools.riscv.megrez_board_session import (
    CRC_RESULT_PATTERN,
    MEGREZ_FRAMEBUFFER,
    MEGREZ_USB_HOST_COMMAND,
    UBOOT_ERROR_PATTERN,
    BoardSession,
    open_serial,
    read_available,
)
from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
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
MAX_BOARD_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    "Uncaught panic",
    "unexpected exception",
    "Kernel panic",
    "Oops:",
)
PROMPT_PATTERN = re.compile(r"(?:^|[\r\n])=> ")
KERNEL_COMPRESSED_ADDRESS = 0x90000000


class BoardTransportError(RuntimeError):
    """One cache validation or XMODEM transport failure."""


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


@dataclass(frozen=True)
class BoardRunConfig:
    """Bounded policy for one physical boot attempt."""

    timeout: float = 300.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 300.0
        ):
            raise ValueError("board timeout must be finite, positive, and at most 300")


class BoardOperations(Protocol):
    """Injected physical operations owned by the production board adapter."""

    def invalidate(self) -> None: ...

    def open(self, timeout: float) -> None: ...

    def ensure_artifacts(self, plan: DebugPlan, timeout: float) -> tuple[str, ...]: ...

    def prepare_boot(self, plan: DebugPlan, timeout: float) -> None: ...

    def booti(self, plan: DebugPlan, timeout: float) -> None: ...

    def read_chunk(self, timeout: float) -> str: ...

    def close(self) -> None: ...

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
        self._output.invalidate("result.json", "serial.log", "transport.json")

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
            f'setenv bootargs "{plan.bootargs}"',
            f'fdt set /chosen bootargs "{plan.bootargs}"',
            MEGREZ_USB_HOST_COMMAND,
        )
        for command in commands:
            session.command(
                command,
                timeout=_remaining(deadline, time.monotonic, phase="uboot-prepare"),
            )

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
) -> StageResult:
    """Run one board attempt without reset or persistent U-Boot writes."""

    operations = RealBoardOperations(plan, device, output_directory)
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
                    ),
                    operations.last_transcript,
                    operations.last_outcomes,
                )
            raise
    finally:
        operations.finish()


class _MarkerTracker:
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = markers
        self._index = 0
        self._tail = ""
        self._tail_limit = (
            max(
                *(len(marker) for marker in markers),
                *(len(marker) for marker in FATAL_MARKERS),
                4,
            )
            - 1
        )

    @property
    def complete(self) -> bool:
        return self._index == len(self._markers)

    @property
    def observed(self) -> int:
        return self._index

    def feed(self, chunk: str) -> bool:
        window = self._tail + chunk
        if any(marker in window for marker in FATAL_MARKERS):
            raise BoardRunFailure("kernel-fatal")

        cursor = 0
        while True:
            occurrences = [
                (position, index)
                for index, marker in enumerate(self._markers)
                if (position := window.find(marker, cursor)) >= 0
            ]
            if not occurrences:
                break
            position, marker_index = min(occurrences)
            if marker_index != self._index:
                raise BoardRunFailure("guest-marker-order")
            marker = self._markers[marker_index]
            self._index += 1
            cursor = position + len(marker)

        recovered = self.complete and PROMPT_PATTERN.search(window, cursor) is not None
        self._tail = window[cursor:][-self._tail_limit :]
        return recovered


def _remaining(deadline: float, clock: Callable[[], float], *, phase: str) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise BoardRunFailure(f"{phase}-timeout")
    return remaining


def _stage_result(plan: DebugPlan, *, passed: bool, reason: str) -> StageResult:
    result = StageResult(
        schema_version=1,
        stage="board",
        passed=passed,
        reason=reason,
        plan_sha256=plan.plan_sha256,
        evidence=("serial.log", "transport.json"),
    )
    result.validate()
    return result


def _serial_timeout_reason(tracker: _MarkerTracker) -> str:
    if tracker.complete:
        return "uboot-recovery-timeout"
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
    deadline = clock() + config.timeout
    tracker = _MarkerTracker(plan.markers)
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
            if tracker.feed(chunk):
                result = _stage_result(plan, passed=True, reason="board-pass")
                break
    except BoardTermination as error:
        pending_termination = error
        result = _stage_result(
            plan, passed=False, reason=f"board-terminated-{error.signum}"
        )
    except BoardTransportError as error:
        result = _stage_result(plan, passed=False, reason=str(error))
    except BoardRunFailure as error:
        result = _stage_result(plan, passed=False, reason=str(error))
    except (OSError, RuntimeError) as error:
        result = _stage_result(
            plan,
            passed=False,
            reason=f"uboot-runtime-{type(error).__name__}",
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
                )
            except (OSError, RuntimeError):
                if pending_termination is None:
                    result = _stage_result(
                        plan, passed=False, reason="transport-close-failed"
                    )

    if result is None:
        result = _stage_result(plan, passed=False, reason="board-internal-error")
    try:
        operations.publish(result, "".join(transcript), outcomes)
    except BoardTermination as error:
        pending_termination = error
        result = _stage_result(
            plan,
            passed=False,
            reason=f"board-terminated-{error.signum}",
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
