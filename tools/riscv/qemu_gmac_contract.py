#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Static contract checks for the EIC7700 GMAC QTest model.

The physical probe and the QEMU model must agree on the bounded read
whitelist.  This module deliberately checks source text only; it never starts
QEMU and never touches a board.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from tools.riscv.eic7700_platform_contract import EIC7700
from tools.riscv.megrez_hardware_probe import PROBE_SPECS


class QemuGmacContractError(ValueError):
    """The QEMU source does not implement the bounded GMAC contract."""


@dataclass(frozen=True)
class QemuGmacSourceContract:
    """Facts extracted from one EIC7700 GMAC qtest source file."""

    gmac_bases: tuple[int, int]
    probe_offsets: tuple[int, ...]
    has_gated_read_write_check: bool
    has_readonly_probe_check: bool
    has_mmio_timeout_check: bool
    has_platform_sequence_check: bool
    has_reset_gate_check: bool
    has_phy_absent_check: bool
    has_unreachable_descriptor_check: bool


_MACRO = re.compile(
    r"^\s*#define\s+(?P<name>[A-Z][A-Z0-9_]*)\s+"
    r"(?P<value>0x[0-9a-fA-F]+|[0-9]+)(?:ULL|UL|LL|U|L)?\b",
    re.MULTILINE,
)
_READONLY_ARRAY = re.compile(
    r"static\s+const\s+uint32_t\s+readonly_offsets\s*\[\s*\]\s*=\s*"
    r"\{(?P<body>.*?)\};",
    re.DOTALL,
)
_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9_]*\b")

_OFFSET_MACROS = (
    "GMAC_MAC_CONFIGURATION",
    "GMAC_MAC_INTERRUPT_STATUS",
    "GMAC_MAC_VERSION",
    "GMAC_MAC_HW_FEATURE1",
    "GMAC_DMA_MODE",
    "GMAC_DMA_STATUS",
)


def _macros(source: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for match in _MACRO.finditer(source):
        values[match.group("name")] = int(match.group("value"), 0)
    return values


def parse_qemu_gmac_probe_offsets(source: str) -> tuple[int, ...]:
    """Extract the qtest read whitelist in source order."""

    array = _READONLY_ARRAY.search(source)
    if array is None:
        raise QemuGmacContractError("QEMU source has no readonly_offsets array")
    names = tuple(_IDENTIFIER.findall(array.group("body")))
    if names != _OFFSET_MACROS:
        raise QemuGmacContractError(
            "QEMU readonly_offsets must contain the six GMAC probe offsets "
            f"in order, got {names!r}"
        )
    values = _macros(source)
    try:
        return tuple(values[name] for name in names)
    except KeyError as error:
        raise QemuGmacContractError(
            f"QEMU source is missing GMAC offset macro {error.args[0]}"
        ) from error


def validate_qemu_gmac_source(source: str) -> QemuGmacSourceContract:
    """Validate the QEMU fail-closed model without executing it."""

    values = _macros(source)
    try:
        bases = (values["GMAC0_BASE"], values["GMAC1_BASE"])
    except KeyError as error:
        raise QemuGmacContractError(
            f"QEMU source is missing {error.args[0]}"
        ) from error
    if bases != (
        EIC7700.peripheral("gmac-0").base,
        EIC7700.peripheral("gmac-1").base,
    ):
        raise QemuGmacContractError(
            f"QEMU GMAC bases {bases!r} disagree with the host registry"
        )

    offsets = parse_qemu_gmac_probe_offsets(source)
    expected = EIC7700.peripheral("gmac-1").probe_offsets
    if offsets != expected:
        raise QemuGmacContractError(
            f"QEMU probe offsets {offsets!r} disagree with Asterinas {expected!r}"
        )

    checks = {
        "has_gated_read_write_check": (
            "test_gmac_clock_gated",
            "UINT32_MAX",
            "qtest_writel(qts, GMAC1_BASE + GMAC_DMA_MODE",
        ),
        "has_readonly_probe_check": (
            "test_gmac_readonly_probe_contract",
            "readonly_offsets",
            "Repeated reads must remain bounded",
        ),
        "has_mmio_timeout_check": (
            "test_gmac_mmio_timeout_fail_closed",
            "eic7700-gmac1-mmio-timeout=on",
            "GMAC_MDIO_BUSY",
            "No PHY transaction, descriptor, or IRQ",
        ),
        "has_platform_sequence_check": (
            "test_gmac_platform_sequence_enables_gated_port",
            "SYSCRG_BASE + 0x148",
            "GMAC1_BASE + GMAC_MAC_VERSION",
        ),
        "has_reset_gate_check": (
            "test_gmac_reset_assertion_closes_gate",
            "SYSCRG_BASE + 0x41c",
            "UINT32_MAX",
        ),
        "has_phy_absent_check": (
            "test_gmac_phy_absent_fail_closed",
            "eic7700-gmac1-phy-absent=on",
            "0xffff",
            "GMAC_DMA_STATUS",
        ),
        "has_unreachable_descriptor_check": (
            "test_gmac_unreachable_descriptor_is_bounded",
            "eic7700-gmac1-irq-drop=on",
            "0xdead0000",
            "qtest_get_irq",
        ),
    }
    results: dict[str, bool] = {}
    for field, needles in checks.items():
        present = all(needle in source for needle in needles)
        if not present:
            raise QemuGmacContractError(
                f"QEMU source is missing fail-closed check {field}"
            )
        results[field] = True
    return QemuGmacSourceContract(
        gmac_bases=bases,
        probe_offsets=offsets,
        **results,
    )
