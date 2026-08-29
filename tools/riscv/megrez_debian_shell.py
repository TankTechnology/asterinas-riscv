#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Host-side commands for the Megrez persistent Debian shell workflow."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import subprocess
import sys

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_debian_shell_contract import PersistentShellPlan
from tools.riscv.megrez_debian_shell_evidence import (
    QemuShellEvidence,
    ShellPermitError,
    validate_qemu_result,
)


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


def main(arguments: Sequence[str] | None = None) -> int:
    """Rejects incomplete CLI usage until the dispatch task is implemented."""

    del arguments
    raise SystemExit("CLI dispatch is not implemented yet")


if __name__ == "__main__":
    main()
