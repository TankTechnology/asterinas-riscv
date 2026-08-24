#!/usr/bin/env python3
"""Patch a Megrez DTB to select the DWC3 USB host for the boot keyboard.

The RockOS DTB does not carry the Asterinas-specific
``/chosen/asterinas,usb-host`` property, so without patching the kernel
falls back to PCI and never drives the on-SoC DWC3 controller. This
tool locates the first ``snps,dwc3`` node in host mode and points the
property at it.

Usage:
    megrez_patch_dtb.py INPUT.dtb OUTPUT.dtb

Returns 0 when a host-mode DWC3 node was found and patched, 1 when the
property already exists (idempotent), and 2 when no suitable node was
found (the DTB is not a Megrez/EIC7700 DTB).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DWC3_COMPATIBLE = "snps,dwc3"
USB_HOST_SELECTOR = "asterinas,usb-host"
EXPECTED_MMIO_STARTS = (0x5048_0000, 0x5049_0000)


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def find_dwc3_nodes(dtb: Path) -> list[str]:
    """Return node paths whose compatible list contains ``snps,dwc3``."""
    listing = run(["fdtget", "-l", str(dtb), "/soc"])
    candidates: list[str] = []
    for name in listing.splitlines():
        node = f"/soc/{name}"
        try:
            compat = run(["fdtget", "-t", "s", str(dtb), node, "compatible"])
        except RuntimeError:
            continue
        if DWC3_COMPATIBLE in compat.split("\0"):
            candidates.append(node)
    return candidates


def is_host_mode(dtb: Path, node: str) -> bool:
    try:
        mode = run(["fdtget", "-t", "s", str(dtb), node, "dr_mode"])
    except RuntimeError:
        # EIC7700 DWC3 nodes may omit dr_mode; the expected MMIO bases
        # disambiguate the two on-SoC controllers only in that case.
        try:
            reg = run(["fdtget", "-t", "x", str(dtb), node, "reg"])
            first = int(reg.split()[0], 16)
        except (RuntimeError, IndexError, ValueError):
            return False
        return first in EXPECTED_MMIO_STARTS
    return mode == "host"


def patch(dtb: Path, output: Path) -> int:
    try:
        existing = run(["fdtget", "-t", "s", str(output), "/chosen", USB_HOST_SELECTOR])
        if existing:
            print(f"already set: {USB_HOST_SELECTOR}={existing}")
            return 1
    except RuntimeError:
        pass

    nodes = find_dwc3_nodes(output)
    if not nodes:
        print(
            f"no {DWC3_COMPATIBLE} node found; is this a Megrez/EIC7700 DTB?",
            file=sys.stderr,
        )
        return 2
    host_nodes = [n for n in nodes if is_host_mode(output, n)]
    if not host_nodes:
        print("no host-mode DWC3 node found", file=sys.stderr)
        return 2
    selected = host_nodes[0]
    run(["fdtput", "-t", "s", str(output), "/chosen", USB_HOST_SELECTOR, selected])
    print(f"patched: {USB_HOST_SELECTOR}={selected}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="input DTB (RockOS DTB)")
    parser.add_argument("output", type=Path, help="patched DTB output path")
    args = parser.parse_args()

    import shutil

    shutil.copyfile(args.input, args.output)
    return patch(args.input, args.output)


if __name__ == "__main__":
    sys.exit(main())
