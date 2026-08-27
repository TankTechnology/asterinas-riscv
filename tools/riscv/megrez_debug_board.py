# SPDX-License-Identifier: MPL-2.0

"""Cache-aware physical transport for one frozen Megrez debug plan."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tools.riscv.megrez_board_session import (
    CRC_RESULT_PATTERN,
    UBOOT_ERROR_PATTERN,
)
from tools.riscv.megrez_debug_contract import (
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_xmodem import INITIAL_BAUD, transfer_fd

BOARD_ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
Command = Callable[[str, float], str]
Transfer = Callable[[int, Path, int], object]
MAX_BOARD_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    "Uncaught panic",
    "unexpected exception",
    "Kernel panic",
    "Oops:",
)
PROMPT_PATTERN = re.compile(r"(?:^|[\r\n])=> ")


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
        if self.status not in ("cache-hit", "transferred"):
            raise ValueError("unknown transport outcome")


def _default_transfer(fd: int, path: Path, address: int) -> object:
    return transfer_fd(fd, path, address, current_baud=INITIAL_BAUD)


class BoardTransport:
    """Verify and populate RAM through one caller-owned serial descriptor."""

    def __init__(
        self,
        *,
        fd: int,
        command: Command,
        transfer: Transfer = _default_transfer,
    ) -> None:
        if type(fd) is not int or fd < 0:
            raise ValueError("serial descriptor must be non-negative")
        self._fd = fd
        self._command = command
        self._transfer = transfer

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
            self._transfer(
                self._fd,
                Path(identity.path),
                identity.load_address,
            )
        except (OSError, RuntimeError) as error:
            raise BoardTransportError(
                f"transport-xmodem: {identity.name}: {error}"
            ) from error
        if self._resident_crc(identity, timeout) != identity.crc32:
            raise BoardTransportError(f"transport-post-transfer-crc: {identity.name}")
        return TransportOutcome(identity.name, "transferred")


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
            except (OSError, RuntimeError):
                if pending_termination is None:
                    result = _stage_result(
                        plan, passed=False, reason="transport-close-failed"
                    )

    if result is None:
        result = _stage_result(plan, passed=False, reason="board-internal-error")
    try:
        operations.publish(result, "".join(transcript), outcomes)
    except (OSError, RuntimeError) as error:
        raise BoardRunFailure("board-publication-failed") from error
    if pending_termination is not None:
        raise pending_termination
    return result
