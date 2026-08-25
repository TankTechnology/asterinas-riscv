#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Exercise one bounded Megrez partition-2 sector and restore its bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

SECTOR_SIZE = 512
P2_START_LBA = 0x000F_A022
P2_NR_SECTORS = 0x0080_0000
TEST_PARTITION_SECTOR = P2_NR_SECTORS - 1
EXPECTED_GPT_SHA256 = "a88a50f1e116c200824cb5660d327f55b06539ab67355a956b65f58398e69f2f"


class GateError(RuntimeError):
    """The reversible write gate could not prove its safety contract."""


@dataclass(frozen=True)
class Preflight:
    partition_start_lba: int
    partition_nr_sectors: int
    gpt_sha256: str
    kernel_write_gate_armed: bool


@dataclass(frozen=True)
class GateResult:
    passed: bool
    physical_lba: int
    original_sha256: str
    nonce_sha256: str
    restored_sha256: str


class SectorOperations(Protocol):
    """Board-side operations required by the reversible write transaction."""

    def preflight(self) -> Preflight: ...

    def read_test_sector(self) -> bytes: ...

    def write_test_sector(self, payload: bytes) -> None: ...

    def sync(self) -> None: ...


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_preflight(preflight: Preflight) -> None:
    if preflight.partition_start_lba != P2_START_LBA:
        raise GateError("partition-2 start LBA mismatch")
    if preflight.partition_nr_sectors != P2_NR_SECTORS:
        raise GateError("partition-2 sector count mismatch")
    if preflight.gpt_sha256 != EXPECTED_GPT_SHA256:
        raise GateError("GPT identity mismatch")
    if not preflight.kernel_write_gate_armed:
        raise GateError("kernel partition-2 write gate is not armed")


def _read_sector(operations: SectorOperations) -> bytes:
    payload = operations.read_test_sector()
    if len(payload) != SECTOR_SIZE:
        raise GateError(f"expected one {SECTOR_SIZE}-byte sector")
    return payload


def _restore(operations: SectorOperations, original: bytes) -> str:
    operations.write_test_sector(original)
    operations.sync()
    restored = _read_sector(operations)
    if restored != original:
        raise GateError("restored sector does not match backup")
    return _sha256(restored)


def exercise(operations: SectorOperations, nonce: bytes) -> GateResult:
    """Write one nonce sector, verify it, and restore the exact original bytes."""

    if len(nonce) != SECTOR_SIZE:
        raise GateError(f"nonce must be exactly {SECTOR_SIZE} bytes")

    preflight = operations.preflight()
    _validate_preflight(preflight)
    original = _read_sector(operations)
    if nonce == original:
        raise GateError("nonce must differ from the original sector")

    restore_required = True
    try:
        operations.write_test_sector(nonce)
        operations.sync()
        observed_nonce = _read_sector(operations)
        if observed_nonce != nonce:
            raise GateError("nonce readback mismatch")
        restored_sha256 = _restore(operations, original)
        restore_required = False
    except BaseException as primary_error:
        if restore_required:
            try:
                _restore(operations, original)
            except BaseException as restore_error:
                raise GateError(
                    f"terminal restore failure: {restore_error}"
                ) from restore_error
        raise primary_error

    return GateResult(
        passed=True,
        physical_lba=P2_START_LBA + TEST_PARTITION_SECTOR,
        original_sha256=_sha256(original),
        nonce_sha256=_sha256(nonce),
        restored_sha256=restored_sha256,
    )
