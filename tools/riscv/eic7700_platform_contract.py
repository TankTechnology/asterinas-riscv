#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Shared, read-only EIC7700 platform contract.

This module is deliberately independent of QEMU, serial I/O, and the kernel.
It is the small source of truth for the facts that a probe and a QTest model
must agree on.  Only register reset values observed in the companion QTest
model are listed; an omitted register is intentionally *not* a claim about
the complete vendor register map.
"""

from __future__ import annotations

from dataclasses import dataclass


DMA_DIRECTION_GUEST_TO_DEVICE = "guest-to-device"
DMA_DIRECTION_DEVICE_TO_GUEST = "device-to-guest"


@dataclass(frozen=True)
class RegisterContract:
    """One register value that is part of the verified reset contract."""

    offset: int
    reset_value: int


@dataclass(frozen=True)
class FailureSemantics:
    """Bounded behavior when a peripheral is unavailable or gated."""

    unavailable_read_value: int
    drop_writes: bool
    suppressed_side_effects: tuple[str, ...]
    bounded: bool


@dataclass(frozen=True)
class PeripheralContract:
    """The cross-layer contract for one EIC7700 peripheral instance."""

    name: str
    base: int
    size: int
    irq: int
    clock_dependencies: tuple[tuple[str, int], ...]
    reset_dependencies: tuple[tuple[str, int], ...]
    registers: tuple[RegisterContract, ...]
    probe_offsets: tuple[int, ...]
    dma_directions: tuple[str, ...]
    failure: FailureSemantics

    def reset_value(self, offset: int) -> int:
        """Return a verified reset value, rejecting unverified offsets."""

        for register in self.registers:
            if register.offset == offset:
                return register.reset_value
        raise KeyError(f"unverified register offset 0x{offset:x}")


class EIC7700:
    """Registry for the EIC7700 instances used by the host gates."""

    _PERIPHERALS = {
        "gmac-0": PeripheralContract(
            name="gmac-0",
            base=0x50400000,
            size=0x10000,
            irq=61,
            clock_dependencies=(
                ("syscrg", 0x148),
                ("syscrg", 0x14C),
                ("hsp", 0x200),
                ("hsp", 0x208),
            ),
            reset_dependencies=(("syscrg", 0x15C), ("syscrg", 0x41C)),
            registers=(
                RegisterContract(0x0000, 0x00000000),
                RegisterContract(0x00B0, 0x00000000),
                RegisterContract(0x0110, 0x00000052),
                RegisterContract(0x0120, 0x00000186),
                RegisterContract(0x1000, 0x00000000),
                RegisterContract(0x1008, 0x00000000),
            ),
            probe_offsets=(0x0000, 0x00B0, 0x0110, 0x0120, 0x1000, 0x1008),
            dma_directions=(
                DMA_DIRECTION_GUEST_TO_DEVICE,
                DMA_DIRECTION_DEVICE_TO_GUEST,
            ),
            failure=FailureSemantics(
                unavailable_read_value=0xFFFFFFFF,
                drop_writes=True,
                suppressed_side_effects=("phy", "mdio", "dma", "tx", "rx", "irq"),
                bounded=True,
            ),
        ),
        "gmac-1": PeripheralContract(
            name="gmac-1",
            base=0x50410000,
            size=0x10000,
            irq=70,
            clock_dependencies=(
                ("syscrg", 0x148),
                ("syscrg", 0x14C),
                ("hsp", 0x200),
                ("hsp", 0x208),
            ),
            reset_dependencies=(("syscrg", 0x15C), ("syscrg", 0x41C)),
            registers=(
                RegisterContract(0x0000, 0x00000000),
                RegisterContract(0x00B0, 0x00000000),
                RegisterContract(0x0110, 0x00000052),
                RegisterContract(0x0120, 0x00000186),
                RegisterContract(0x1000, 0x00000000),
                RegisterContract(0x1008, 0x00000000),
            ),
            probe_offsets=(0x0000, 0x00B0, 0x0110, 0x0120, 0x1000, 0x1008),
            dma_directions=(
                DMA_DIRECTION_GUEST_TO_DEVICE,
                DMA_DIRECTION_DEVICE_TO_GUEST,
            ),
            failure=FailureSemantics(
                unavailable_read_value=0xFFFFFFFF,
                drop_writes=True,
                suppressed_side_effects=("phy", "mdio", "dma", "tx", "rx", "irq"),
                bounded=True,
            ),
        ),
        "dwc3-0": PeripheralContract(
            name="dwc3-0",
            base=0x50480000,
            size=0x10000,
            irq=85,
            clock_dependencies=(),
            reset_dependencies=(),
            registers=(
                RegisterContract(0x00C110, 0x30C13004),
                RegisterContract(0x00C120, 0x5533330A),
                RegisterContract(0x00C400, 0x00000000),
            ),
            probe_offsets=(0x10, 0xC110, 0xC120),
            dma_directions=(
                DMA_DIRECTION_GUEST_TO_DEVICE,
                DMA_DIRECTION_DEVICE_TO_GUEST,
            ),
            failure=FailureSemantics(
                unavailable_read_value=0,
                drop_writes=False,
                suppressed_side_effects=("xhci", "device-enumeration", "irq"),
                bounded=True,
            ),
        ),
        "dwc3-1": PeripheralContract(
            name="dwc3-1",
            base=0x50490000,
            size=0x10000,
            irq=86,
            clock_dependencies=(),
            reset_dependencies=(),
            registers=(
                RegisterContract(0x00C110, 0x30C13004),
                RegisterContract(0x00C120, 0x5533330A),
                RegisterContract(0x00C400, 0x00000000),
            ),
            probe_offsets=(0x10, 0xC110, 0xC120),
            dma_directions=(
                DMA_DIRECTION_GUEST_TO_DEVICE,
                DMA_DIRECTION_DEVICE_TO_GUEST,
            ),
            failure=FailureSemantics(
                unavailable_read_value=0,
                drop_writes=False,
                suppressed_side_effects=("xhci", "device-enumeration", "irq"),
                bounded=True,
            ),
        ),
    }

    @classmethod
    def peripheral(cls, name: str) -> PeripheralContract:
        """Return one registered peripheral or fail closed."""

        return cls._PERIPHERALS[name]

