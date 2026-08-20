#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Closed suite definitions for the RISC-V LTP gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LtpSuite:
    """A reviewed requested manifest and its exact packaging counts."""

    name: str
    enabled: Path
    expected_selected: int
    expected_unavailable: int


_SUITE_SPECS = (
    (
        "syscalls",
        Path("test/initramfs/src/conformance/ltp/testcases/all.txt"),
        767,
        12,
    ),
    (
        "arch-riscv64",
        Path("tools/riscv/ltp/manifests/arch-riscv64.txt"),
        138,
        1,
    ),
)


def suite_names() -> tuple[str, ...]:
    """Returns every accepted suite name in stable CLI order."""

    return tuple(name for name, _, _, _ in _SUITE_SPECS)


def suite_by_name(repo: Path, name: str) -> LtpSuite:
    """Resolves one reviewed suite below the bound repository."""

    for suite_name, relative, selected, unavailable in _SUITE_SPECS:
        if name == suite_name:
            return LtpSuite(
                name=suite_name,
                enabled=repo.resolve() / relative,
                expected_selected=selected,
                expected_unavailable=unavailable,
            )
    raise ValueError(f"unknown LTP suite: {name}")
