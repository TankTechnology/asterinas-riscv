#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Static contract for the EIC7700 DWC3 fault-isolation qtests.

The runtime qtests live in the companion QEMU checkout.  This small parser
keeps the important negative-test shape visible to the Asterinas host gate:
reset, IRQ-without-event-ring, and unimplemented-register reads must not add a
USB device or write guest DMA memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


class QemuDwc3ContractError(ValueError):
    """The QEMU source does not preserve the DWC3 fault contract."""


@dataclass(frozen=True)
class QemuDwc3SourceContract:
    """Facts extracted from the DWC3 qtest source."""

    functions: tuple[str, ...]
    registrations: tuple[str, ...]
    no_device_or_dma_in_faults: bool


_FUNCTION_START = re.compile(
    r"static\s+void\s+(?P<name>test_dwc3_fault_[a-z0-9_]+)\s*\([^)]*\)\s*\{",
    re.MULTILINE,
)
_REGISTRATION = re.compile(
    r'qtest_add_func\("/eic7700/dwc3/(?P<name>fault-[a-z0-9-]+)"\s*,\s*'
    r"(?P<function>test_dwc3_fault_[a-z0-9_]+)\s*\);",
    re.MULTILINE,
)

_EXPECTED = {
    "test_dwc3_fault_reset_isolates_xhci": (
        "DWC3_GCTL",
        "XHCI_USBCMD",
        "XHCI_USBSTS",
        "XHCI_IMAN",
        "XHCI_ERSTSZ",
        "qtest_get_irq",
    ),
    "test_dwc3_fault_irq_without_event_ring_isolated": (
        "XHCI_IMAN",
        "XHCI_ERSTBA_LO",
        "XHCI_ERDP_LO",
        "XHCI_USBSTS",
        "qtest_get_irq",
    ),
    "test_dwc3_fault_register_read_is_fail_closed": (
        "DWC3_GUSB2I2CCTL",
        "XHCI_USBCMD",
        "XHCI_IMAN",
        "XHCI_ERSTSZ",
        "qtest_get_irq",
    ),
}


def _function_bodies(source: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for match in _FUNCTION_START.finditer(source):
        depth = 1
        index = match.end()
        while depth and index < len(source):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise QemuDwc3ContractError(
                f"unterminated DWC3 fault function {match.group('name')}"
            )
        bodies[match.group("name")] = source[match.start() : index]
    return bodies


def validate_qemu_dwc3_source(source: str) -> QemuDwc3SourceContract:
    """Validate the three runtime fault tests and their registrations."""

    bodies = _function_bodies(source)
    expected_names = tuple(_EXPECTED)
    if tuple(bodies) != expected_names:
        raise QemuDwc3ContractError(
            f"DWC3 fault functions must be {expected_names!r}, got {tuple(bodies)!r}"
        )
    for name, needles in _EXPECTED.items():
        body = bodies[name]
        if "qtest_qmp_device_add" in body:
            raise QemuDwc3ContractError(f"{name} enumerates a USB device")
        if "qtest_memwrite" in body or "qtest_memread" in body:
            raise QemuDwc3ContractError(f"{name} performs guest DMA memory I/O")
        missing = tuple(needle for needle in needles if needle not in body)
        if missing:
            raise QemuDwc3ContractError(f"{name} misses {missing!r}")

    registrations = tuple(
        match.group("function") for match in _REGISTRATION.finditer(source)
    )
    if registrations != expected_names:
        raise QemuDwc3ContractError(
            f"DWC3 fault tests are not registered in order: {registrations!r}"
        )
    return QemuDwc3SourceContract(
        functions=expected_names,
        registrations=registrations,
        no_device_or_dma_in_faults=True,
    )
