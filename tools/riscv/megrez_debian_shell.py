#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Host-side commands for the Megrez persistent Debian shell workflow."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_debian_shell_contract import (
    P2_NR_SECTORS,
    P2_START_LBA,
    SHELL_ARTIFACT_ORDER,
    FrozenArtifact,
    PersistentShellPlan,
    ShellContractError,
    validate_rootfs_identity,
)
from tools.riscv.megrez_debian_shell_evidence import (
    QemuShellEvidence,
    ShellPermit,
    ShellPermitError,
    issue_shell_permit,
    validate_qemu_result,
)
from tools.riscv.megrez_debian_shell_board import InventoryError, InventoryResult
from tools.riscv.megrez_debian_shell_physical import (
    PhysicalShellError,
    PhysicalShellResult,
)


_REPOSITORY = Path(__file__).resolve().parents[2]
_TARGET = _REPOSITORY / "target"
_INTERFACE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}\Z")
_DEVICE_RE = re.compile(r"\A/dev/[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


class ShellWorkflowError(RuntimeError):
    """The operator command is unsafe or inconsistent with frozen evidence."""


def qemu_gate_argv(plan: PersistentShellPlan, output: Path) -> tuple[str, ...]:
    """Builds the exact generic-Sv39 rootfs-gate command."""

    plan.validate()
    if not output.is_absolute() or not output.is_dir() or output.is_symlink():
        raise ShellPermitError("QEMU output must be an absolute non-symlink directory")
    files = plan.artifact_map()
    return (
        sys.executable,
        "-m",
        "tools.riscv.debian.rootfs.rootfs_gate",
        "--kernel",
        files["qemu_kernel"].path,
        "--uboot",
        files["qemu_uboot"].path,
        "--dtb",
        files["qemu_dtb"].path,
        "--stage1-initramfs",
        files["stage1"].path,
        "--root-image",
        files["root_image"].path,
        "--root-manifest",
        files["root_manifest"].path,
        "--packages-lock",
        files["packages_lock"].path,
        "--package-checksums",
        files["package_checksums"].path,
        "--output-directory",
        str(output),
        "--smp",
        "4",
    )


def run_qemu_gate(
    plan: PersistentShellPlan,
    output: Path,
    *,
    evidence_path: Path | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> QemuShellEvidence:
    """Runs one QEMU gate and publishes shell-level evidence last."""

    evidence_path = evidence_path or output / "qemu-evidence.json"
    if not evidence_path.is_absolute() or evidence_path.is_symlink():
        raise ShellPermitError("QEMU evidence output must be an absolute regular path")
    try:
        with PinnedOutputDirectory(evidence_path.parent) as evidence_output:
            evidence_output.invalidate(evidence_path.name)
            argv = qemu_gate_argv(plan, output)
            run_command(argv, check=True)
            evidence = validate_qemu_result(plan, output / "result.json")
            evidence_output.atomic_write(
                evidence_path.name,
                evidence.canonical_bytes(),
            )
            return evidence
    except ShellPermitError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise ShellPermitError(f"QEMU gate failed: {error}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="freeze one dual-platform bundle")
    for name in SHELL_ARTIFACT_ORDER:
        plan.add_argument(
            f"--{name.replace('_', '-')}", required=True, type=_input_file
        )
    plan.add_argument("--output", required=True, type=_output_path)

    check = commands.add_parser("check", help="validate a canonical frozen bundle")
    check.add_argument("plan", type=_input_file)

    qemu = commands.add_parser("qemu", help="run the two-boot generic-Sv39 gate")
    qemu.add_argument("plan", type=_input_file)
    qemu.add_argument("--output", required=True, type=_output_path)

    permit = commands.add_parser("permit", help="bind QEMU evidence to this commit")
    permit.add_argument("plan", type=_input_file)
    permit.add_argument("--qemu-evidence", required=True, type=_input_file)
    permit.add_argument("--output", required=True, type=_output_path)

    inventory = commands.add_parser("inventory", help="measure partition 2 read-only")
    _physical_common(inventory, permit=True, output=True)
    inventory.add_argument("--prior-inventory", type=_input_file)
    inventory.add_argument("--install-result", type=_input_file)

    install = commands.add_parser(
        "install-if-needed", help="write only a measured-mismatching partition 2"
    )
    _physical_common(install, permit=True, output=True, interface=False)
    install.add_argument("--inventory", required=True, type=_input_file)

    gate = commands.add_parser("gate", help="run two bounded physical shell boots")
    _physical_common(gate, permit=True, output=True)
    gate.add_argument("--inventory", required=True, type=_input_file)

    handoff = commands.add_parser("handoff", help="leave one passing shell running")
    _physical_common(handoff, permit=False, output=False)
    handoff.add_argument("--result", required=True, type=_input_file)
    return parser


def _physical_common(
    parser: argparse.ArgumentParser,
    *,
    permit: bool,
    output: bool,
    interface: bool = True,
) -> None:
    parser.add_argument("plan", type=_input_file)
    parser.add_argument("device", type=_device)
    if permit:
        parser.add_argument("--permit", required=True, type=_input_file)
    if output:
        parser.add_argument("--output", required=True, type=_output_path)
    if interface:
        parser.add_argument("--host-interface", default="enp12s0", type=_interface)
    parser.add_argument("--deadline", type=_positive_finite, default=660.0)
    parser.add_argument("--yes", action="store_true", required=True)


def _input_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("input paths must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise argparse.ArgumentTypeError(f"input is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise argparse.ArgumentTypeError("inputs must be non-symlink regular files")
    return path


def _output_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or Path(os.path.normpath(value)) != path:
        raise argparse.ArgumentTypeError("output paths must be canonical and absolute")
    try:
        path.relative_to(_TARGET)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "outputs must be below repository target"
        ) from error
    current = _TARGET
    for component in path.relative_to(_TARGET).parts:
        current /= component
        if current.is_symlink():
            raise argparse.ArgumentTypeError("output paths must not traverse symlinks")
    return path


def _interface(value: str) -> str:
    if _INTERFACE_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("unsafe host interface")
    return value


def _device(value: str) -> str:
    if _DEVICE_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("unsafe serial device")
    return value


def _positive_finite(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 3600:
        raise argparse.ArgumentTypeError("deadline must be in (0, 3600]")
    return parsed


def load_plan(path: Path) -> PersistentShellPlan:
    return PersistentShellPlan.from_bytes(_read_regular(path, "plan"))


def _load_permit(path: Path) -> ShellPermit:
    return ShellPermit.from_bytes(_read_regular(path, "permit"))


def _load_inventory(path: Path) -> InventoryResult:
    return InventoryResult.from_bytes(_read_regular(path, "inventory"))


def _read_regular(path: Path, role: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8 * 1024 * 1024:
            raise ShellWorkflowError(f"{role} is not a bounded regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        if not payload or len(payload) != metadata.st_size:
            raise ShellWorkflowError(f"{role} changed while reading")
        return payload
    finally:
        os.close(descriptor)


def create_plan(values: argparse.Namespace) -> PersistentShellPlan:
    commit = _clean_git_commit(_REPOSITORY)
    artifacts = tuple(
        FrozenArtifact.from_path(name, getattr(values, name))
        for name in SHELL_ARTIFACT_ORDER
    )
    plan = PersistentShellPlan(
        schema_version=1,
        git_commit=commit,
        artifacts=artifacts,
        smp=4,
        qemu_paging="sv39",
        megrez_paging="sv48",
        gate_bootargs=(
            "console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.reboot_after=180"
        ),
        final_bootargs="console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init",
        gate_reboot_after=180,
        long_operation_reboot_after=600,
        partition_start_lba=P2_START_LBA,
        partition_nr_sectors=P2_NR_SECTORS,
    )
    validate_rootfs_identity(plan)
    values.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with PinnedOutputDirectory(values.output.parent) as output:
        output.atomic_write(values.output.name, plan.canonical_bytes())
    return plan


def _clean_git_commit(repository: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if status:
        raise ShellWorkflowError("tracked working tree must be clean")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _prepare_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ShellWorkflowError("output must be a non-symlink directory")
    return path


def run_inventory_from_cli(values: argparse.Namespace) -> object:
    from tools.riscv.megrez_debian_shell_cli_io import run_inventory_command

    return run_inventory_command(values)


def install_from_cli(values: argparse.Namespace) -> object:
    from tools.riscv.megrez_debian_shell_cli_io import run_install_command

    return run_install_command(values)


def run_gate_from_cli(values: argparse.Namespace) -> object:
    from tools.riscv.megrez_debian_shell_physical_io import run_physical_board_gate

    return run_physical_board_gate(
        load_plan(values.plan),
        _load_permit(values.permit),
        _load_inventory(values.inventory),
        device=values.device,
        interface=values.host_interface,
        output=_prepare_directory(values.output),
    )


def handoff_physical_shell(values: argparse.Namespace) -> object:
    from tools.riscv.megrez_debian_shell_cli_io import run_handoff_command

    return run_handoff_command(values)


def main(arguments: Sequence[str] | None = None) -> int:
    """Dispatch one exact operator stage without embedding protocol logic."""

    values = _parser().parse_args(arguments)
    try:
        if values.command == "plan":
            create_plan(values)
        elif values.command == "check":
            validate_rootfs_identity(load_plan(values.plan))
        elif values.command == "qemu":
            run_qemu_gate(load_plan(values.plan), _prepare_directory(values.output))
        elif values.command == "permit":
            issue_shell_permit(
                load_plan(values.plan),
                values.qemu_evidence,
                values.output,
                repository=_REPOSITORY,
            )
        elif values.command == "inventory":
            run_inventory_from_cli(values)
        elif values.command == "install-if-needed":
            install_from_cli(values)
        elif values.command == "gate":
            run_gate_from_cli(values)
        else:
            result = PhysicalShellResult.from_bytes(
                _read_regular(values.result, "result")
            )
            if not result.passed:
                raise PhysicalShellError("handoff requires a passing physical result")
            handoff_physical_shell(values)
    except (
        InventoryError,
        OSError,
        PhysicalShellError,
        ShellContractError,
        ShellPermitError,
        ShellWorkflowError,
        subprocess.SubprocessError,
    ) as error:
        print(f"megrez-debian-shell: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    main()
