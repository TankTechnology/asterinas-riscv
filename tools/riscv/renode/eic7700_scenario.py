#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Fast, deterministic EIC7700 fault-sequence model for Renode replay.

The model is intentionally smaller than QEMU: it checks ordering and
fail-closed invariants over the platform-control state machine.  QEMU remains
the authority for MMIO timing and DMA behavior.  This file can run standalone
on hosts without Renode and emits the same scenario names used by the Renode
``.resc`` adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import json
from pathlib import Path
from typing import Iterable


FAULTS = (
    "clock-gated",
    "reset-asserted",
    "phy-absent",
    "mmio-timeout",
    "descriptor-unreachable",
    "irq-drop",
)
GMAC1_BASE = 0x5041_0000
MAC_VERSION = 0x0110
DMA_STATUS = 0x1008


@dataclass
class Eic7700State:
    faults: frozenset[str] = frozenset()
    platform_ready: bool = False
    mdio_reads: int = 0
    descriptor_submissions: int = 0
    irq_attempts: int = 0
    irq_assertions: int = 0
    trace: list[str] = field(default_factory=list)

    def platform_init(self) -> None:
        self.trace.append("platform-init")
        self.platform_ready = True

    def read_gmac1(self, offset: int) -> int:
        self.trace.append("read:0x{:04x}".format(offset))
        if not self.platform_ready or {
            "clock-gated",
            "reset-asserted",
            "mmio-timeout",
        } & self.faults:
            return 0xFFFF_FFFF
        if offset == MAC_VERSION:
            return 0x1052
        if offset == DMA_STATUS:
            return 0
        return 0

    def mdio_read(self) -> int:
        self.trace.append("mdio-read")
        self.mdio_reads += 1
        return 0xFFFF if "phy-absent" in self.faults else 0x1234

    def submit_descriptor(self) -> bool:
        self.trace.append("dma-submit")
        self.descriptor_submissions += 1
        return "descriptor-unreachable" not in self.faults

    def raise_irq(self) -> bool:
        self.trace.append("irq")
        self.irq_attempts += 1
        self.irq_assertions += 1
        return "irq-drop" not in self.faults

    def recover_watchdog(self) -> None:
        self.trace.append("watchdog-recovery")
        self.platform_ready = False


def run_scenario(faults: Iterable[str]) -> dict[str, object]:
    selected = frozenset(faults)
    unknown = selected - set(FAULTS)
    if unknown:
        raise ValueError("unknown faults: {}".format(sorted(unknown)))
    state = Eic7700State(selected)
    state.platform_init()
    version = state.read_gmac1(MAC_VERSION)
    mdio = 0xFFFF
    descriptor_ok = False
    irq_seen = False
    if version != 0xFFFF_FFFF:
        mdio = state.mdio_read()
        if mdio != 0xFFFF:
            descriptor_ok = state.submit_descriptor()
            if descriptor_ok:
                irq_seen = state.raise_irq()
    unavailable = version == 0xFFFF_FFFF
    state.recover_watchdog()

    # A fault may change observable results, but it must never create a
    # second descriptor or leave the platform armed after recovery.
    if state.descriptor_submissions > 1 or state.irq_attempts > 1 or state.platform_ready:
        raise AssertionError("recovery invariant violated")
    return {
        "faults": sorted(selected),
        "version": version,
        "mdio": mdio,
        "mdio_reads": state.mdio_reads,
        "descriptor_ok": descriptor_ok,
        "irq_seen": irq_seen,
        "irq_attempts": state.irq_attempts,
        "unavailable": unavailable,
        "descriptor_submissions": state.descriptor_submissions,
        "trace": state.trace,
    }


def all_fault_combinations() -> Iterable[frozenset[str]]:
    for size in range(len(FAULTS) + 1):
        yield from (frozenset(item) for item in itertools.combinations(FAULTS, size))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--fault", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    scenarios = all_fault_combinations() if args.matrix else [frozenset(args.fault)]
    results = [run_scenario(faults) for faults in scenarios]
    payload = "\n".join(json.dumps(result, sort_keys=True) for result in results) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
