#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Physical-board inventory for the Megrez persistent Debian shell."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Protocol

from tools.riscv.megrez_board_session import PartitionGeometry
from tools.riscv.megrez_debian_shell_contract import (
    P2_NR_SECTORS,
    P2_START_LBA,
    FrozenArtifact,
    PersistentShellPlan,
)
from tools.riscv.megrez_debian_shell_evidence import ShellPermit
from tools.riscv.megrez_debug_contract import StageResult
from tools.riscv.megrez_debian_install import (
    INSTALLER_FILENAME,
    NetworkInstallRequest,
)
from tools.riscv.megrez_preboard import _PinnedPermitOutput


_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
_PARTITION_SIZE_BYTES = 4 * 1024 * 1024 * 1024
_ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_REASON_RE = re.compile(r"\A[a-z][a-z0-9-]*\Z")
_RESULT_KEYS = {
    "schema_version",
    "status",
    "reason",
    "plan_sha256",
    "permit_sha256",
    "partitions",
    "expected_root_sha256",
    "install_result_sha256",
    "serial_sha256",
}
_PARTITION_KEYS = {"number", "start_lba", "nr_sectors"}
_STATUSES = {"matching", "needs-install", "not-measurable"}
_READY_MARKER = (
    "DEBIAN_INVENTORY_READY target=/dev/mmcblk0p2 "
    f"bytes={_PARTITION_SIZE_BYTES} write=disabled"
)
InstallRunner = Callable[[NetworkInstallRequest], StageResult]
ArtifactReader = Callable[[str, Path], FrozenArtifact]


class InventoryError(ValueError):
    """Physical root identity cannot be measured safely."""


class InventoryOperations(Protocol):
    """Side effects required by :func:`run_inventory`."""

    def invalidate(self) -> None: ...

    def read_partition_geometry(self) -> tuple[PartitionGeometry, ...]: ...

    def matching_install_result(
        self,
        plan: PersistentShellPlan,
        permit: ShellPermit,
        geometry: tuple[PartitionGeometry, ...],
    ) -> str | None: ...

    def run_verifier(self, bootargs: str) -> bytes: ...

    def publish(self, result: InventoryResult) -> None: ...


@dataclass(frozen=True)
class InventoryResult:
    """The canonical result of one read-only partition inventory."""

    schema_version: int
    status: str
    reason: str
    plan_sha256: str
    permit_sha256: str
    partitions: tuple[PartitionGeometry, ...]
    expected_root_sha256: str
    install_result_sha256: str | None
    serial_sha256: str | None

    def validate(self) -> None:
        _exact_integer(self.schema_version, 1, "schema_version")
        if self.status not in _STATUSES:
            raise InventoryError(f"unsupported inventory status: {self.status!r}")
        if (
            not isinstance(self.reason, str)
            or _REASON_RE.fullmatch(self.reason) is None
        ):
            raise InventoryError("inventory reason must be a stable lowercase token")
        _sha256(self.plan_sha256, "plan_sha256")
        _sha256(self.permit_sha256, "permit_sha256")
        _sha256(self.expected_root_sha256, "expected_root_sha256")
        _optional_sha256(self.install_result_sha256, "install_result_sha256")
        _optional_sha256(self.serial_sha256, "serial_sha256")
        _validate_geometry(self.partitions, allow_empty=self.status == "not-measurable")
        if self.status == "matching":
            if self.reason == "install-result":
                if self.install_result_sha256 is None or self.serial_sha256 is not None:
                    raise InventoryError(
                        "install-result inventory has inconsistent evidence"
                    )
            elif self.reason == "verified-root":
                if self.install_result_sha256 is not None or self.serial_sha256 is None:
                    raise InventoryError(
                        "verified-root inventory has inconsistent evidence"
                    )
            else:
                raise InventoryError("matching inventory has an invalid reason")
        elif self.status == "needs-install":
            if (
                self.reason != "image-hash"
                or self.install_result_sha256 is not None
                or self.serial_sha256 is None
            ):
                raise InventoryError("needs-install requires exact image-hash evidence")
        elif self.reason not in {
            "partition-geometry",
            "install-result",
            "verifier-failed",
            "verifier-evidence",
        }:
            raise InventoryError("not-measurable inventory has an invalid reason")
        elif self.install_result_sha256 is not None:
            raise InventoryError(
                "not-measurable inventory cannot trust an install result"
            )

    def canonical_bytes(self) -> bytes:
        self.validate()
        document = {
            "schema_version": self.schema_version,
            "status": self.status,
            "reason": self.reason,
            "plan_sha256": self.plan_sha256,
            "permit_sha256": self.permit_sha256,
            "partitions": [
                {
                    "number": partition.number,
                    "start_lba": partition.start_lba,
                    "nr_sectors": partition.nr_sectors,
                }
                for partition in self.partitions
            ],
            "expected_root_sha256": self.expected_root_sha256,
            "install_result_sha256": self.install_result_sha256,
            "serial_sha256": self.serial_sha256,
        }
        return (
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> InventoryResult:
        document = _load_json(payload)
        raw_partitions = document["partitions"]
        if not isinstance(raw_partitions, list):
            raise InventoryError("partitions must be an array")
        partitions = []
        for index, value in enumerate(raw_partitions):
            partition = _mapping(value, f"partition {index}")
            if set(partition) != _PARTITION_KEYS:
                raise InventoryError(f"partition {index} fields differ")
            partitions.append(
                PartitionGeometry(
                    number=_integer(partition["number"], f"partition {index} number"),
                    start_lba=_integer(
                        partition["start_lba"], f"partition {index} start_lba"
                    ),
                    nr_sectors=_integer(
                        partition["nr_sectors"], f"partition {index} nr_sectors"
                    ),
                )
            )
        result = cls(
            schema_version=_integer(document["schema_version"], "schema_version"),
            status=_string(document["status"], "status"),
            reason=_string(document["reason"], "reason"),
            plan_sha256=_string(document["plan_sha256"], "plan_sha256"),
            permit_sha256=_string(document["permit_sha256"], "permit_sha256"),
            partitions=tuple(partitions),
            expected_root_sha256=_string(
                document["expected_root_sha256"], "expected_root_sha256"
            ),
            install_result_sha256=_optional_string(
                document["install_result_sha256"], "install_result_sha256"
            ),
            serial_sha256=_optional_string(document["serial_sha256"], "serial_sha256"),
        )
        result.validate()
        if result.canonical_bytes() != payload:
            raise InventoryError("inventory result must use canonical JSON")
        return result


def classify_inventory_log(text: str, expected_sha256: str) -> str:
    """Classifies one complete verifier transcript without inferring success."""

    _sha256(expected_sha256, "expected root SHA-256")
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_TRANSCRIPT_BYTES:
        raise InventoryError("inventory transcript exceeds 8 MiB")
    lines = text.replace("\r", "").splitlines()
    if lines.count(_READY_MARKER) != 1:
        raise InventoryError("inventory readiness marker is missing or ambiguous")
    ready_index = text.index(_READY_MARKER)
    lower = text.lower()
    reboot_positions = [
        position
        for marker in ("reboot: restarting system", "rebooting in")
        if (position := lower.find(marker)) >= 0
    ]
    if reboot_positions and min(reboot_positions) < ready_index:
        raise InventoryError("verifier rebooted before inventory readiness")
    if "kernel panic" in lower:
        raise InventoryError("verifier transcript contains a kernel panic")
    pass_marker = (
        f"DEBIAN_VERIFY_PASS sha256={expected_sha256} bytes={_ROOT_IMAGE_SIZE_BYTES}"
    )
    fail_marker = "DEBIAN_VERIFY_FAIL reason=image-hash"
    pass_count = lines.count(pass_marker)
    fail_count = lines.count(fail_marker)
    any_pass_count = sum(line.startswith("DEBIAN_VERIFY_PASS") for line in lines)
    any_fail_count = sum(line.startswith("DEBIAN_VERIFY_FAIL") for line in lines)
    if (
        pass_count == 1
        and fail_count == 0
        and any_pass_count == 1
        and any_fail_count == 0
        and text.index(pass_marker) > ready_index
    ):
        return "matching"
    if (
        fail_count == 1
        and pass_count == 0
        and any_fail_count == 1
        and any_pass_count == 0
        and text.index(fail_marker) > ready_index
    ):
        return "needs-install"
    raise InventoryError("partition root identity was not measurable")


def verifier_bootargs(plan: PersistentShellPlan) -> str:
    """Returns the exact read-only verifier kernel arguments."""

    plan.validate()
    return " ".join(
        (
            "console=ttyS0",
            "cpu_no_boost_1_6ghz",
            "loglevel=info",
            "init=/init",
            f"asterinas.reboot_after={plan.long_operation_reboot_after}",
        )
    )


def installer_bootargs(plan: PersistentShellPlan, root_sha256: str) -> str:
    """Returns the exact partition-2 write arguments for the long installer."""

    plan.validate()
    _sha256(root_sha256, "root SHA-256")
    if root_sha256 != plan.artifact_map()["root_image"].sha256:
        raise InventoryError("installer root identity differs from the plan")
    return " ".join(
        (
            "console=ttyS0",
            "cpu_no_boost_1_6ghz",
            "loglevel=info",
            "init=/init",
            "asterinas.net=eic7700-rj45,10.100.19.200/21",
            ("asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e"),
            "asterinas.mmc_write_partition2",
            f"asterinas.debian_install_sha256={root_sha256}",
            f"asterinas.reboot_after={plan.long_operation_reboot_after}",
        )
    )


def install_if_needed(
    plan: PersistentShellPlan,
    permit: ShellPermit,
    inventory: InventoryResult,
    output: Path,
    *,
    repository: Path,
    run: InstallRunner,
    artifact_reader: ArtifactReader = FrozenArtifact.from_path,
) -> StageResult:
    """Skips a matching root or consumes one permit for one install attempt."""

    with _PinnedPermitOutput(output / "result.json", repository) as publication:
        publication.invalidate()
        _validate_install_inputs(plan, permit, inventory)
        if inventory.status == "matching":
            result = StageResult(
                1,
                "install",
                True,
                "already-matching",
                plan.plan_sha256,
                ("inventory.json",),
            )
            result.validate()
            publication.write(result.canonical_bytes())
            return result
        if inventory.status != "needs-install":
            raise InventoryError("only a measured mismatch may authorize installation")

        artifacts = plan.artifact_map()
        for name in ("megrez_kernel", "installer_base", "megrez_dtb", "root_image"):
            if artifact_reader(name, Path(artifacts[name].path)) != artifacts[name]:
                raise InventoryError(f"install artifact changed: {name}")
        kernel = artifacts["megrez_kernel"]
        request = NetworkInstallRequest(
            plan_sha256=plan.plan_sha256,
            git_commit=plan.git_commit,
            kernel=Path(kernel.path),
            kernel_size=kernel.size,
            kernel_crc32=kernel.crc32,
            installer_base=Path(artifacts["installer_base"].path),
            megrez_dtb_crc32=artifacts["megrez_dtb"].crc32,
            root_image=Path(artifacts["root_image"].path),
            root_sha256=artifacts["root_image"].sha256,
            reboot_after=plan.long_operation_reboot_after,
            bootargs=installer_bootargs(plan, artifacts["root_image"].sha256),
        )
        request.validate()
        permit_sha256 = hashlib.sha256(permit.canonical_bytes()).hexdigest()
        inventory_sha256 = hashlib.sha256(inventory.canonical_bytes()).hexdigest()
        attempt = StageResult(
            1,
            "install",
            False,
            "attempt-started",
            plan.plan_sha256,
            (
                f"permit-sha256:{permit_sha256}",
                f"inventory-sha256:{inventory_sha256}",
                f"root-sha256:{request.root_sha256}",
            ),
        )
        with _PinnedPermitOutput(output / "attempt.json", repository) as attempt_file:
            attempt_file.write(attempt.canonical_bytes())
            result = run(request)
        _validate_install_result(plan, result)
        publication.write(result.canonical_bytes())
        return result


def run_inventory(
    plan: PersistentShellPlan,
    permit: ShellPermit,
    operations: InventoryOperations,
) -> InventoryResult:
    """Runs one fail-closed read-only inventory and publishes its result."""

    operations.invalidate()
    _validate_prerequisites(plan, permit)
    permit_sha256 = hashlib.sha256(permit.canonical_bytes()).hexdigest()
    expected_root_sha256 = plan.artifact_map()["root_image"].sha256
    geometry: tuple[PartitionGeometry, ...] = ()
    install_result_sha256 = None
    serial_sha256 = None
    status = "not-measurable"
    reason = "partition-geometry"
    try:
        candidate_geometry = tuple(operations.read_partition_geometry())
        _validate_geometry(candidate_geometry, allow_empty=False)
        geometry = candidate_geometry
        reason = "install-result"
        install_result_sha256 = operations.matching_install_result(
            plan, permit, geometry
        )
        if install_result_sha256 is not None:
            _sha256(install_result_sha256, "install result SHA-256")
            status = "matching"
        else:
            reason = "verifier-failed"
            transcript = operations.run_verifier(verifier_bootargs(plan))
            if not isinstance(transcript, bytes):
                raise InventoryError("verifier transcript must be bytes")
            serial_sha256 = hashlib.sha256(transcript).hexdigest()
            reason = "verifier-evidence"
            classification = classify_inventory_log(
                transcript.decode("utf-8", errors="strict"),
                expected_root_sha256,
            )
            if classification == "matching":
                status = "matching"
                reason = "verified-root"
            else:
                status = "needs-install"
                reason = "image-hash"
    except (InventoryError, OSError, RuntimeError, UnicodeDecodeError):
        status = "not-measurable"
    result = InventoryResult(
        schema_version=1,
        status=status,
        reason=reason,
        plan_sha256=plan.plan_sha256,
        permit_sha256=permit_sha256,
        partitions=geometry,
        expected_root_sha256=expected_root_sha256,
        install_result_sha256=(install_result_sha256 if status == "matching" else None),
        serial_sha256=serial_sha256,
    )
    result.validate()
    operations.publish(result)
    return result


def _validate_install_inputs(
    plan: PersistentShellPlan,
    permit: ShellPermit,
    inventory: InventoryResult,
) -> None:
    _validate_prerequisites(plan, permit)
    inventory.validate()
    permit_sha256 = hashlib.sha256(permit.canonical_bytes()).hexdigest()
    root_sha256 = plan.artifact_map()["root_image"].sha256
    if (
        inventory.plan_sha256 != plan.plan_sha256
        or inventory.permit_sha256 != permit_sha256
        or inventory.expected_root_sha256 != root_sha256
    ):
        raise InventoryError("inventory differs from the current plan and permit")
    _validate_geometry(inventory.partitions, allow_empty=False)


def _validate_install_result(plan: PersistentShellPlan, result: StageResult) -> None:
    if not isinstance(result, StageResult):
        raise InventoryError("installer returned an invalid result type")
    result.validate()
    if (
        result.stage != "install"
        or result.passed is not True
        or result.reason != "install-pass"
        or result.plan_sha256 != plan.plan_sha256
        or result.evidence != ("installer.serial.log", INSTALLER_FILENAME)
    ):
        raise InventoryError("installer result differs from the frozen request")


def _validate_prerequisites(plan: PersistentShellPlan, permit: ShellPermit) -> None:
    plan.validate()
    permit.validate()
    artifacts = plan.artifact_map()
    if (
        permit.plan_sha256 != plan.plan_sha256
        or permit.git_commit != plan.git_commit
        or permit.megrez_kernel_sha256 != artifacts["megrez_kernel"].sha256
        or permit.stage1_crc32 != artifacts["stage1"].crc32
        or permit.megrez_dtb_crc32 != artifacts["megrez_dtb"].crc32
        or permit.root_image_sha256 != artifacts["root_image"].sha256
        or permit.gate_bootargs != plan.gate_bootargs
        or permit.gate_reboot_after != plan.gate_reboot_after
        or permit.long_operation_reboot_after != plan.long_operation_reboot_after
    ):
        raise InventoryError("shell permit differs from the frozen plan")


def _validate_geometry(
    partitions: tuple[PartitionGeometry, ...], *, allow_empty: bool
) -> None:
    if not isinstance(partitions, tuple):
        raise InventoryError("partition geometry must be an immutable tuple")
    if not partitions and allow_empty:
        return
    if len(partitions) != 3 or any(
        not isinstance(partition, PartitionGeometry) for partition in partitions
    ):
        raise InventoryError("partition geometry must contain partitions 1, 2, and 3")
    if tuple(partition.number for partition in partitions) != (1, 2, 3):
        raise InventoryError("partition geometry is out of order")
    for partition in partitions:
        for field, value in (
            ("start_lba", partition.start_lba),
            ("nr_sectors", partition.nr_sectors),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InventoryError(f"partition {partition.number} {field} is invalid")
    if (partitions[1].start_lba, partitions[1].nr_sectors) != (
        P2_START_LBA,
        P2_NR_SECTORS,
    ):
        raise InventoryError("partition 2 differs from the frozen write extent")
    if (
        partitions[0].start_lba + partitions[0].nr_sectors > partitions[1].start_lba
        or partitions[1].start_lba + partitions[1].nr_sectors > partitions[2].start_lba
    ):
        raise InventoryError("partition geometry overlaps")


def _load_json(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InventoryError("inventory result must be UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError as error:
        raise InventoryError("invalid inventory result JSON") from error
    document = _mapping(value, "inventory result")
    if set(document) != _RESULT_KEYS:
        raise InventoryError("inventory result fields differ")
    return document


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InventoryError(f"{role} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise InventoryError(f"{field} must be a string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InventoryError(f"{field} must be an integer")
    return value


def _exact_integer(value: object, expected: int, field: str) -> None:
    if _integer(value, field) != expected:
        raise InventoryError(f"{field} must be {expected}")


def _sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InventoryError(f"{field} must be lowercase hexadecimal")


def _optional_sha256(value: object, field: str) -> None:
    if value is not None:
        _sha256(value, field)
