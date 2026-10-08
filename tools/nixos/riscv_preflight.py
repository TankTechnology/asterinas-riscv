#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

"""Validate RISC-V NixOS boot inputs and render a QEMU command."""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


_CPU = "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"
_MEMORY = "2G"
_MACHINE = "virt"
_SMP = 4


@dataclass(frozen=True)
class ArtifactContract:
    """Artifacts required by the fixed RISC-V NixOS runner contract."""

    uboot: Path
    boot_disk: Path
    root_disk: Path
    dtb: Path

    @classmethod
    def from_repo(cls, repo: Path) -> "ArtifactContract":
        """Construct the fixed artifact layout rooted at ``repo``."""

        if not isinstance(repo, Path):
            raise ValueError("repository path must be a pathlib.Path")
        return cls(
            uboot=repo / "target/qemu-uboot/cache/u-boot-build/u-boot",
            boot_disk=repo / "target/nixos/riscv64/boot.ext4",
            root_disk=repo / "target/nixos/riscv64/root.ext2",
            dtb=repo / "target/nixos/riscv64/qemu-virt.dtb",
        )


@dataclass(frozen=True)
class PreflightFailure:
    """One unusable artifact and the action that owns its production."""

    kind: str
    path: Path
    remedy: str


_ARTIFACT_REMEDIES = {
    "uboot": (
        "the producer requires a nonempty Sv39 Image and nonempty initramfs; "
        "first run tools/riscv/prepare_qemu_uboot_booti.sh --check-tools, then "
        "run ASTERINAS_RISCV_BOOTI=<nonempty Sv39 Image> "
        "ASTERINAS_INITRAMFS=<nonempty initramfs> "
        "tools/riscv/prepare_qemu_uboot_booti.sh prepare"
    ),
    "boot-disk": "implement or run the R1-B boot-disk producer (not yet available)",
    "root-disk": (
        "produce the R1-A NixOS closure/root prerequisite, then implement or run "
        "the R1-B root-disk producer (not yet available)"
    ),
    "dtb": "implement or run the R1-B DTB producer (not yet available)",
}


def _artifact_failure(kind: str, path: Path, reason: str) -> PreflightFailure:
    return PreflightFailure(
        kind=kind,
        path=path,
        remedy=f"artifact is {reason}; {_ARTIFACT_REMEDIES[kind]}",
    )


def check_artifacts(contract: ArtifactContract) -> tuple[PreflightFailure, ...]:
    """Perform a point-in-time, render-only artifact check in fixed order.

    Each path is opened once. The nonblocking, read-only open follows symlinks.
    Validation uses ``fstat`` on the descriptor, which is always closed in a
    ``finally`` block without reading its contents. Checks across artifacts are
    not atomic, and this function does not authenticate or freeze artifacts
    against replacement during or after the check.
    """

    failures: list[PreflightFailure] = []
    disk_metadata: dict[str, os.stat_result] = {}
    for kind, _, path in _artifact_descriptors(contract):
        try:
            file_descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK,
            )
        except FileNotFoundError:
            failures.append(_artifact_failure(kind, path, "missing"))
            continue
        except OSError as error:
            failures.append(_artifact_failure(kind, path, f"unreadable ({error})"))
            continue

        try:
            metadata = os.fstat(file_descriptor)
        except OSError as error:
            failures.append(_artifact_failure(kind, path, f"not inspectable ({error})"))
        else:
            if kind in ("boot-disk", "root-disk"):
                disk_metadata[kind] = metadata
            if not stat.S_ISREG(metadata.st_mode):
                failures.append(_artifact_failure(kind, path, "not a regular file"))
            elif metadata.st_size == 0:
                failures.append(_artifact_failure(kind, path, "empty"))
        finally:
            os.close(file_descriptor)

    boot_metadata = disk_metadata.get("boot-disk")
    root_metadata = disk_metadata.get("root-disk")
    if (
        boot_metadata is not None
        and root_metadata is not None
        and (boot_metadata.st_dev, boot_metadata.st_ino)
        == (root_metadata.st_dev, root_metadata.st_ino)
    ):
        failures.append(
            PreflightFailure(
                kind="disk-alias",
                path=contract.root_disk,
                remedy=(
                    "point-in-time metadata samples resolve boot disk "
                    f"{contract.boot_disk} and root disk {contract.root_disk} "
                    "to the same underlying file; produce distinct disk images"
                ),
            )
        )
    return tuple(failures)


def _artifact_descriptors(
    contract: ArtifactContract,
) -> tuple[tuple[str, str, Path], ...]:
    descriptors = (
        ("uboot", "U-Boot", contract.uboot),
        ("boot-disk", "boot disk", contract.boot_disk),
        ("root-disk", "root disk", contract.root_disk),
        ("dtb", "DTB", contract.dtb),
    )
    for _, display_name, path in descriptors:
        if not isinstance(path, Path):
            raise ValueError(f"{display_name} path must be a pathlib.Path")
    return descriptors


def _validate_contract(contract: ArtifactContract) -> None:
    _artifact_descriptors(contract)
    if contract.boot_disk == contract.root_disk:
        raise ValueError("boot and root disk paths must be distinct")
    for name, path in (
        ("boot disk", contract.boot_disk),
        ("root disk", contract.root_disk),
    ):
        if "," in str(path):
            raise ValueError(f"{name} path must not contain a comma: {path}")


def _validate_qemu(qemu: str) -> None:
    if not isinstance(qemu, str) or not qemu.strip():
        raise ValueError("QEMU program must be a nonempty string")


def qemu_argv(
    contract: ArtifactContract,
    *,
    qemu: str = "qemu-system-riscv64",
) -> list[str]:
    """Build the deterministic QEMU argv without inspecting or running it."""

    _validate_contract(contract)
    _validate_qemu(qemu)
    return [
        qemu,
        "-machine",
        _MACHINE,
        "-cpu",
        _CPU,
        "-m",
        _MEMORY,
        "-smp",
        str(_SMP),
        "-no-reboot",
        "-kernel",
        str(contract.uboot),
        "-dtb",
        str(contract.dtb),
        "-drive",
        f"if=none,format=raw,file={contract.boot_disk},id=bootdisk,snapshot=on",
        "-device",
        "virtio-blk-device,drive=bootdisk",
        "-drive",
        f"if=none,format=raw,file={contract.root_disk},id=rootdisk,snapshot=on",
        "-device",
        "virtio-blk-device,drive=rootdisk",
        "-device",
        "virtio-gpu-device",
        "-device",
        "virtio-keyboard-device",
        "-device",
        "virtio-tablet-device",
        "-netdev",
        "user,id=net0",
        "-device",
        "virtio-net-device,netdev=net0",
        "-serial",
        "stdio",
        "-display",
        "gtk",
    ]


def _default_repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_args(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check RISC-V NixOS boot artifacts or print the QEMU command "
            "without executing it."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate artifacts")
    mode.add_argument(
        "--print-qemu",
        action="store_true",
        help="validate artifacts and print a shell-safe QEMU command",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=_default_repo(),
        help="repository root (default: detected from this script)",
    )
    parser.add_argument(
        "--qemu",
        default="qemu-system-riscv64",
        help="QEMU executable to render",
    )
    return parser.parse_args(arguments)


def _print_failures(failures: Sequence[PreflightFailure]) -> None:
    for failure in failures:
        print(
            f"preflight failure: {failure.kind}: {failure.path}: {failure.remedy}",
            file=sys.stderr,
        )


def main(arguments: Sequence[str] | None = None) -> int:
    """Run an artifact check or render-only QEMU preflight."""

    options = _parse_args(arguments)
    try:
        contract = ArtifactContract.from_repo(options.repo)
        _validate_contract(contract)
        if options.print_qemu:
            _validate_qemu(options.qemu)
        failures = check_artifacts(contract)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if failures:
        _print_failures(failures)
        return 2
    if options.check:
        print("RISC-V NixOS preflight OK: all artifacts are nonempty regular files")
    else:
        print(shlex.join(qemu_argv(contract, qemu=options.qemu)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
