# SPDX-License-Identifier: MPL-2.0

"""Create and validate one simulation-first Megrez debug plan."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import tempfile
from contextlib import AbstractContextManager
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from tools.riscv.debian.rootfs.contract import (
    ContractError,
    load_manifest,
    load_package_checksums,
    validate_frozen_root,
)
from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_ARTIFACT_ORDER,
    DEBIAN_BROWSER_MARKERS,
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_board import (
    BoardRunFailure,
    BoardTermination,
    run_physical_board,
)
from tools.riscv.megrez_debug_desktop import (
    DesktopSimulationError,
    simulate_desktop,
)
from tools.riscv.megrez_debug_simulation import SimulationError, simulate_fast
from tools.riscv.megrez_debug_probe import (
    PROBE_STRESS_SIZES,
    ProbeServer,
    ProbeServerError,
)
from tools.riscv.megrez_debian_install import InstallError, run_network_install
from tools.riscv.megrez_preboard import (
    PreboardError,
    RecoveryEvidence,
    create_recovery_evidence,
    issue_preboard_permit,
)

KERNEL_ADDRESS = 0x80200000
INITRAMFS_ADDRESS = 0x83000000
DTB_ADDRESS = 0xF0000000
MAX_PLAN_BYTES = 1024 * 1024


class WorkflowError(RuntimeError):
    """One stable workflow failure that must not touch the board."""


def _board_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not 0 < timeout <= 300 or not math.isfinite(timeout):
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 300]")
    return timeout


def _install_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not 0 < timeout <= 3600 or not math.isfinite(timeout):
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 3600]")
    return timeout


def _read_regular(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise WorkflowError(f"{label}-missing: {error}") from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_PLAN_BYTES:
            raise WorkflowError(f"{label}-invalid: expected a bounded regular file")
        data = bytearray()
        while len(data) <= MAX_PLAN_BYTES:
            chunk = os.read(fd, min(64 * 1024, MAX_PLAN_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != opened.st_size:
            raise WorkflowError(f"{label}-invalid: file size changed while reading")
        return bytes(data)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise WorkflowError("plan-output-unsafe")
    parent = path.parent
    if parent.exists():
        parent_info = parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise WorkflowError("plan-output-parent-unsafe")
    else:
        parent.mkdir(parents=True, mode=0o755)

    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(temporary_fd, 0o644)
        view = memoryview(payload)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise WorkflowError("plan-output-short-write")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_plan(path: Path) -> DebugPlan:
    try:
        return DebugPlan.from_bytes(_read_regular(path, label="plan"))
    except DebugContractError as error:
        raise WorkflowError(f"plan-invalid: {error}") from error


def _check_artifacts(plan: DebugPlan) -> None:
    for expected in plan.artifacts:
        try:
            actual = ArtifactIdentity.from_path(
                expected.name, Path(expected.path), expected.load_address
            )
        except (DebugContractError, OSError) as error:
            raise WorkflowError(
                f"plan-artifact-drift: {expected.name}: {error}"
            ) from error
        if actual != expected:
            raise WorkflowError(f"plan-artifact-drift: {expected.name}")


def _create_plan(arguments: argparse.Namespace) -> DebugPlan:
    boot_artifacts = (
        ArtifactIdentity.from_path("kernel", arguments.kernel, KERNEL_ADDRESS),
        ArtifactIdentity.from_path("initramfs", arguments.initramfs, INITRAMFS_ADDRESS),
        ArtifactIdentity.from_path("qemu_dtb", arguments.qemu_dtb, DTB_ADDRESS),
        ArtifactIdentity.from_path("megrez_dtb", arguments.megrez_dtb, DTB_ADDRESS),
    )
    if arguments.profile == "tcp-probe":
        if not arguments.marker:
            raise WorkflowError("plan-markers-required")
        artifacts = boot_artifacts
        schema_version = 1
        markers = tuple(arguments.marker)
    else:
        if arguments.marker:
            raise WorkflowError("plan-browser-markers-are-profile-defined")
        required = {
            name: getattr(arguments, name)
            for name in DEBIAN_BROWSER_ARTIFACT_ORDER[len(boot_artifacts) :]
        }
        missing = sorted(name for name, path in required.items() if path is None)
        if missing:
            raise WorkflowError(f"plan-missing-debian-artifacts: {missing}")
        try:
            manifest = load_manifest(required["root_manifest"])
            manifest = validate_frozen_root(
                required["root_image"], manifest, required["packages_lock"]
            )
            package_rows = load_package_checksums(required["package_checksums"])
        except (ContractError, OSError) as error:
            raise WorkflowError(f"plan-rootfs-invalid: {error}") from error
        if manifest.profile != "desktop-m5-network":
            raise WorkflowError("plan-rootfs-profile-mismatch")
        if package_rows != manifest.downloaded_packages:
            raise WorkflowError("plan-package-checksums-mismatch")
        evidence_artifacts = tuple(
            ArtifactIdentity.from_path(name, required[name], 0)
            for name in DEBIAN_BROWSER_ARTIFACT_ORDER[len(boot_artifacts) :]
        )
        evidence_by_name = {identity.name: identity for identity in evidence_artifacts}
        expected_hashes = {
            "root_image": manifest.root_image_sha256,
            "packages_lock": manifest.packages_lock_sha256,
            "in_release": manifest.signed_metadata_sha256,
        }
        if any(
            evidence_by_name[name].sha256 != expected
            for name, expected in expected_hashes.items()
        ):
            raise WorkflowError("plan-rootfs-provenance-mismatch")
        artifacts = (*boot_artifacts, *evidence_artifacts)
        schema_version = 2
        markers = DEBIAN_BROWSER_MARKERS
    plan = DebugPlan(
        schema_version=schema_version,
        profile=arguments.profile,
        artifacts=artifacts,
        bootargs=arguments.bootargs,
        smp=4,
        sv39=getattr(arguments, "paging_mode", "sv39") == "sv39",
        markers=markers,
        reboot_after=arguments.reboot_after,
    )
    plan.validate()
    return plan


def _physical_actions(
    plan: DebugPlan, *, hardware_watchdog: bool = False
) -> list[dict[str, object]]:
    identities = {identity.name: identity for identity in plan.artifacts}
    actions: list[dict[str, object]] = [
        {"action": "require-simulation", "tier": "fast"},
        {"action": "probe-uboot-baud", "choices": [115200, 1500000]},
    ]
    for name in ("kernel", "initramfs", "megrez_dtb"):
        identity = identities[name]
        actions.append(
            {
                "action": "cache-or-transfer",
                "artifact": name,
                "address": identity.load_address,
            }
        )
    if hardware_watchdog:
        actions.append(
            {"action": "arm-hardware-watchdog", "mode": "interrupt-then-reset"}
        )
    actions.extend(
        (
            {"action": "boot-once", "reboot_after": plan.reboot_after},
            {"action": "capture-markers"},
            {"action": "await-automatic-recovery"},
        )
    )
    return actions


def _validate_simulation(path: Path, plan: DebugPlan) -> None:
    try:
        result = StageResult.from_bytes(_read_regular(path, label="plan-simulation"))
    except DebugContractError as error:
        raise WorkflowError(f"plan-simulation-invalid: {error}") from error
    expected_stage = (
        "desktop"
        if plan.schema_version == 2 and plan.profile == "debian-browser"
        else "fast"
    )
    if (
        result.stage != expected_stage
        or not result.passed
        or result.plan_sha256 != plan.plan_sha256
    ):
        raise WorkflowError("plan-simulation-mismatch")


def _validate_recovery(path: Path, plan: DebugPlan) -> None:
    try:
        result = RecoveryEvidence.from_bytes(_read_regular(path, label="plan-recovery"))
    except PreboardError as error:
        raise WorkflowError(f"plan-recovery-invalid: {error}") from error
    kernel = next(
        (identity for identity in plan.artifacts if identity.name == "kernel"),
        None,
    )
    if (
        kernel is None
        or not result.passed
        or result.reason != "recovery-pass"
        or result.plan_sha256 != plan.plan_sha256
        or result.kernel_sha256 != kernel.sha256
        or not result.second_firmware_epoch
        or not result.fresh_uboot_prompt
    ):
        raise WorkflowError("plan-recovery-mismatch")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="freeze one exact artifact set")
    plan.add_argument(
        "--profile", choices=("tcp-probe", "debian-browser"), default="tcp-probe"
    )
    plan.add_argument("--kernel", required=True, type=Path)
    plan.add_argument("--initramfs", required=True, type=Path)
    plan.add_argument("--qemu-dtb", required=True, type=Path)
    plan.add_argument("--megrez-dtb", required=True, type=Path)
    plan.add_argument("--u-boot", dest="u_boot", type=Path)
    plan.add_argument("--root-image", type=Path)
    plan.add_argument("--root-manifest", type=Path)
    plan.add_argument("--packages-lock", type=Path)
    plan.add_argument("--package-checksums", type=Path)
    plan.add_argument("--in-release", type=Path)
    plan.add_argument("--bootargs", required=True)
    plan.add_argument("--paging-mode", choices=("sv39", "sv48"), default="sv39")
    plan.add_argument("--marker", action="append")
    plan.add_argument("--reboot-after", type=int, default=180)
    plan.add_argument("--output", required=True, type=Path)

    check = subparsers.add_parser("check", help="revalidate every plan artifact")
    check.add_argument("plan", type=Path)

    simulate = subparsers.add_parser("simulate", help="run a plan-bound QEMU gate")
    simulate.add_argument("plan", type=Path)
    simulate.add_argument("--tier", choices=("fast", "desktop"), required=True)
    simulate.add_argument("--output-directory", required=True, type=Path)
    simulate.add_argument("--uboot-build-directory", type=Path)

    recovery = subparsers.add_parser(
        "recovery", help="bind a software-reboot QEMU result to a plan"
    )
    recovery.add_argument("plan", type=Path)
    recovery.add_argument("--native-result", required=True, type=Path)
    recovery.add_argument("--serial-log", required=True, type=Path)
    recovery.add_argument("--sha256sums", required=True, type=Path)
    recovery.add_argument("--output", required=True, type=Path)

    preboard = subparsers.add_parser(
        "preboard", help="issue a QEMU-backed physical boot permit"
    )
    preboard.add_argument("plan", type=Path)
    preboard.add_argument("--desktop-result", required=True, type=Path)
    preboard.add_argument("--recovery-result", required=True, type=Path)
    preboard.add_argument("--output", required=True, type=Path)

    install = subparsers.add_parser(
        "install", help="install the permitted Debian root through Asterinas"
    )
    install.add_argument("plan", type=Path)
    install.add_argument("device")
    install.add_argument("--permit", required=True, type=Path)
    install.add_argument("--output-directory", required=True, type=Path)
    install.add_argument("--base-cpio", required=True, type=Path)
    install.add_argument("--tftp-directory", required=True, type=Path)
    install.add_argument("--root-url", required=True)
    install.add_argument("--timeout", type=_install_timeout)

    board = subparsers.add_parser("board", help="show or execute physical actions")
    board.add_argument("plan", type=Path)
    board.add_argument("device")
    board.add_argument("--simulation-result", required=True, type=Path)
    board.add_argument("--recovery-result", type=Path)
    board.add_argument("--output-directory", type=Path)
    board.add_argument("--timeout", type=_board_timeout, default=300.0)
    board.add_argument("--hardware-watchdog", action="store_true")
    board.add_argument("--dry-run", action="store_true")
    return parser


def _ordered_probe_server() -> ProbeServer:
    return ProbeServer(payload_sizes=PROBE_STRESS_SIZES)


def main(
    arguments: Sequence[str] | None = None,
    *,
    probe_server_factory: Callable[
        [], AbstractContextManager[object]
    ] = _ordered_probe_server,
) -> int:
    try:
        values = _parser().parse_args(arguments)
        if values.command == "plan":
            plan = _create_plan(values)
            _atomic_write(values.output, plan.canonical_bytes())
            return 0
        if values.command == "check":
            plan = _load_plan(values.plan)
            _check_artifacts(plan)
            print(f"MEGREZ_DEBUG_CHECK_PASS plan={plan.plan_sha256}")
            return 0
        if values.command == "simulate":
            plan = _load_plan(values.plan)
            if values.tier == "fast":
                if values.uboot_build_directory is None:
                    raise WorkflowError(
                        "fast-simulation-uboot-build-directory-required"
                    )
                _check_artifacts(plan)
                with probe_server_factory():
                    result = simulate_fast(
                        plan,
                        values.output_directory,
                        values.uboot_build_directory,
                    )
            else:
                result = simulate_desktop(
                    plan,
                    values.output_directory,
                )
            _atomic_write(
                values.output_directory / "result.json", result.canonical_bytes()
            )
            return 0
        if values.command == "recovery":
            plan = _load_plan(values.plan)
            result = create_recovery_evidence(
                plan,
                values.native_result,
                values.serial_log,
                values.sha256sums,
            )
            _atomic_write(values.output, result.canonical_bytes())
            return 0
        if values.command == "preboard":
            plan = _load_plan(values.plan)
            issue_preboard_permit(
                plan,
                values.desktop_result,
                values.recovery_result,
                values.output,
            )
            return 0
        if values.command == "install":
            plan = _load_plan(values.plan)
            run_network_install(
                plan,
                values.permit,
                values.device,
                values.output_directory,
                values.base_cpio,
                values.tftp_directory,
                values.root_url,
                timeout=values.timeout,
            )
            return 0

        plan = _load_plan(values.plan)
        if values.dry_run:
            print(
                json.dumps(
                    _physical_actions(plan, hardware_watchdog=values.hardware_watchdog),
                    separators=(",", ":"),
                )
            )
            return 0
        _check_artifacts(plan)
        _validate_simulation(values.simulation_result, plan)
        if values.recovery_result is None:
            raise WorkflowError("board-recovery-result-required")
        _validate_recovery(values.recovery_result, plan)
        if values.output_directory is None:
            raise WorkflowError("board-output-directory-required")
        with probe_server_factory() as probe_server:
            trace_provider = getattr(probe_server, "canonical_trace", None)
            result = run_physical_board(
                plan,
                values.device,
                values.output_directory,
                timeout=values.timeout,
                hardware_watchdog=values.hardware_watchdog,
                **(
                    {
                        "probe_trace_provider": lambda plan_sha256: trace_provider(
                            plan_sha256=plan_sha256
                        )
                    }
                    if callable(trace_provider)
                    else {}
                ),
            )
        return 0 if result.passed else 2
    except BoardTermination as error:
        return 128 + error.signum
    except (
        BoardRunFailure,
        DebugContractError,
        DesktopSimulationError,
        InstallError,
        OSError,
        PreboardError,
        ProbeServerError,
        SimulationError,
        WorkflowError,
    ) as error:
        print(f"megrez-debug: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
