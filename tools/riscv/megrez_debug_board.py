# SPDX-License-Identifier: MPL-2.0

"""Cache-aware physical transport for one frozen Megrez debug plan."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tools.riscv.megrez_board_session import (
    CRC_RESULT_PATTERN,
    UBOOT_ERROR_PATTERN,
)
from tools.riscv.megrez_debug_contract import ArtifactIdentity, DebugPlan
from tools.riscv.megrez_xmodem import INITIAL_BAUD, transfer_fd

BOARD_ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
Command = Callable[[str, float], str]
Transfer = Callable[[int, Path, int], object]


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
