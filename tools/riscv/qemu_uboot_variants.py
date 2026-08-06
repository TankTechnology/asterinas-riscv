#!/usr/bin/env python3
"""Immutable diagnostic variants for guarded RISC-V U-Boot runs."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from qemu_uboot_profiles import QemuUbootProfile, validate_registered_profile


@dataclass(frozen=True)
class QemuUbootVariant:
    """One registered diagnostic change to a QEMU U-Boot profile."""

    name: str
    base_profile_name: str
    bootarg_suffix: str
    payload_uart_compatible: str
    terminal_stage: str
    completion_line: bytes
    expected_userspace_marker_count: int
    classification: str
    transport: str


FIRST_PROCESS_CONSOLE_LOSS = QemuUbootVariant(
    name="first-process-console-loss",
    base_profile_name="megrez-sv48-svade-fast",
    bootarg_suffix="asterinas.first_process_diag=1",
    payload_uart_compatible="snps,dw-apb-uart",
    terminal_stage="user_first_write_returned",
    completion_line=(
        b"ASTERINAS_FIRST_PROCESS_DIAG "
        b"stage=user_first_write_returned "
        b"fd=1 requested=50 result=50"
    ),
    expected_userspace_marker_count=0,
    classification="EXPECTED_CONSOLE_ROUTE_LOSS",
    transport="U-Boot booti only",
)

_VARIANTS: Mapping[str, QemuUbootVariant] = MappingProxyType(
    {FIRST_PROCESS_CONSOLE_LOSS.name: FIRST_PROCESS_CONSOLE_LOSS}
)


def variant_by_name(name: str) -> QemuUbootVariant:
    """Resolves a registered diagnostic variant by its stable name."""

    try:
        return _VARIANTS[name]
    except KeyError as error:
        raise ValueError(f"unknown QEMU U-Boot variant: {name}") from error


def validate_registered_variant(variant: QemuUbootVariant) -> None:
    """Rejects variants that are not the registered singleton object."""

    try:
        registered = variant_by_name(variant.name)
    except ValueError as error:
        raise ValueError(
            f"variant is not a registered singleton: {variant.name}"
        ) from error
    if variant is not registered:
        raise ValueError(f"variant is not a registered singleton: {variant.name}")


def effective_bootargs(
    profile: QemuUbootProfile,
    variant: QemuUbootVariant | None,
) -> str:
    """Returns boot arguments for a registered profile and optional variant."""

    validate_registered_profile(profile)
    if variant is None:
        return profile.bootargs

    validate_registered_variant(variant)
    if profile.name != variant.base_profile_name:
        raise ValueError(
            f"variant {variant.name} requires base profile "
            f"{variant.base_profile_name}, not {profile.name}"
        )
    return f"{profile.bootargs} {variant.bootarg_suffix}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser(
        "validate",
        help="validate one registered diagnostic variant",
    )
    validate_parser.add_argument(
        "--variant",
        type=variant_by_name,
        required=True,
        help="registered diagnostic variant name",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    validate_registered_variant(args.variant)
    print(f"validated QEMU U-Boot variant: {args.variant.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
