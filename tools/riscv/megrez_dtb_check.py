#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Check a Megrez / EIC7700 device tree against what this kernel actually uses.

The board's device tree comes from the vendor firmware and from RockOS, not
from this repository, so the first board boot can fail before any driver runs:
a missing `stdout-path`, a missing `/memory` region, or an initrd range that
only one of its two endpoints describes are all silent-or-panic failures. This
tool reads a `.dtb` (or a `.dts`, compiled with `dtc`) and reports every node
and property the kernel parses, so the tree can be checked before power-on.

The checks mirror the kernel, not the specification:

* `/cpus` and `/memory` validation follows `ostd/src/arch/riscv/boot/mod.rs`
  (`validate_cpu_nodes`, `validate_memory_layout`) and the initramfs range
  parser (`linux,initrd-start` / `linux,initrd-end` must come as a pair).
* `stdout-path` selection follows
  `kernel/comps/uart/src/arch/riscv/mod.rs`: `snps,dw-apb-uart`,
  `sifive,uart0`, or `ns16550a`.
* Device nodes follow the drivers that exist for RISC-V in this tree:
  `eswin,sdhci-sdio` (mmc, requires the `eswin,syscrg_csr` phandle),
  `snps,dwmac` with its `snps,axi-config`/`snps,blen` description,
  `pci-host-ecam-generic`, and `simple-framebuffer`.

Usage:

    python3 tools/riscv/megrez_dtb_check.py board.dtb
    python3 tools/riscv/megrez_dtb_check.py board.dtb --json
    python3 tools/riscv/megrez_dtb_check.py board.dts --verbose

Exit status is 0 when nothing is missing at `fail` level, 1 otherwise. `warn`
findings do not fail the check: they are things the kernel can boot without
(network, display) or things to confirm on the bench.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9

UART_COMPATIBLES = ("snps,dw-apb-uart", "sifive,uart0", "ns16550a")
PLIC_COMPATIBLES = (
    "riscv,plic0",
    "sifive,plic-1.0.0",
    "thead,c900-plic",
    "andestech,nceplic100",
    "eswin,eic7700-plic",
)
MMC_COMPATIBLE = "eswin,sdhci-sdio"
GMAC_COMPATIBLE_PREFIX = "snps,dwmac"
PCI_COMPATIBLE = "pci-host-ecam-generic"
FRAMEBUFFER_COMPATIBLE = "simple-framebuffer"


class DtbError(Exception):
    pass


@dataclass
class Node:
    name: str
    props: dict[str, bytes] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None

    def path(self) -> str:
        parts = []
        node: Node | None = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts)) if parts else "/"

    def child(self, name: str) -> "Node | None":
        for candidate in self.children:
            if candidate.name == name:
                return candidate
        return None

    def walk(self):
        yield self
        for candidate in self.children:
            yield from candidate.walk()

    def string(self, prop: str) -> str | None:
        raw = self.props.get(prop)
        if raw is None:
            return None
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    def string_list(self, prop: str) -> list[str]:
        raw = self.props.get(prop)
        if raw is None:
            return []
        return [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]

    def cells(self, prop: str) -> list[int] | None:
        raw = self.props.get(prop)
        if raw is None or len(raw) % 4 != 0:
            return None
        return list(struct.unpack(f">{len(raw) // 4}I", raw))

    def compatible(self) -> list[str]:
        return self.string_list("compatible")


def parse_dtb(blob: bytes) -> Node:
    if len(blob) < 40:
        raise DtbError("file is too small to be a device tree")
    (magic, totalsize, off_struct, off_strings, _off_rsvmap, version,
     _last_comp, _boot_cpuid, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
    if magic != FDT_MAGIC:
        raise DtbError(f"bad FDT magic 0x{magic:08x}")
    if totalsize > len(blob):
        raise DtbError(f"header totalsize {totalsize} exceeds file size {len(blob)}")
    strings = blob[off_strings:off_strings + size_strings]
    struct_block = blob[off_struct:off_struct + size_struct]

    root = Node(name="")
    stack = [root]
    offset = 0

    def read_u32() -> int:
        nonlocal offset
        value = struct.unpack_from(">I", struct_block, offset)[0]
        offset += 4
        return value

    def read_cstr() -> str:
        nonlocal offset
        end = struct_block.index(b"\0", offset)
        text = struct_block[offset:end].decode("utf-8", "replace")
        offset = (end + 1 + 3) & ~3
        return text

    while offset < len(struct_block):
        token = read_u32()
        if token == FDT_BEGIN_NODE:
            name = read_cstr()
            node = Node(name=name, parent=stack[-1])
            stack[-1].children.append(node)
            stack.append(node)
        elif token == FDT_END_NODE:
            if len(stack) == 1:
                raise DtbError("unbalanced FDT_END_NODE at the root")
            stack.pop()
        elif token == FDT_PROP:
            length = read_u32()
            name_off = read_u32()
            end = strings.index(b"\0", name_off)
            prop_name = strings[name_off:end].decode("utf-8", "replace")
            value = struct_block[offset:offset + length]
            offset = (offset + length + 3) & ~3
            stack[-1].props[prop_name] = bytes(value)
        elif token == FDT_NOP:
            continue
        elif token == FDT_END:
            break
        else:
            raise DtbError(f"unknown FDT token 0x{token:x} (version {version})")

    # The first FDT_BEGIN_NODE is the tree root; the holder above only exists
    # to keep the parser's stack logic uniform.
    if not root.children:
        raise DtbError("device tree has no root node")
    real_root = root.children[0]
    real_root.parent = None
    return real_root


@dataclass
class Finding:
    level: str  # "ok" | "warn" | "fail"
    item: str
    detail: str


class Checker:
    def __init__(self, root: Node):
        self.root = root
        self.findings: list[Finding] = []

    def add(self, level: str, item: str, detail: str) -> None:
        self.findings.append(Finding(level, item, detail))

    def ok(self, item: str, detail: str) -> None:
        self.add("ok", item, detail)

    def fail(self, item: str, detail: str) -> None:
        self.add("fail", item, detail)

    def warn(self, item: str, detail: str) -> None:
        self.add("warn", item, detail)

    # -- individual checks -------------------------------------------------

    def check_cpus(self) -> None:
        cpus = self.root.child("cpus")
        if cpus is None:
            self.fail("cpus", "no /cpus node")
            return
        harts: list[int] = []
        for node in cpus.children:
            if node.name.split("@", 1)[0] != "cpu":
                continue
            if node.string("device_type") != "cpu":
                continue
            if "mmu-type" not in node.props:
                continue  # management harts are skipped by the kernel too
            cells = node.cells("reg")
            harts.append(cells[0] if cells else -1)
        if not harts:
            self.fail("cpus", "/cpus describes no MMU-capable CPU")
            return
        duplicates = {hart for hart in harts if harts.count(hart) > 1}
        if -1 in harts:
            self.fail("cpus", "a CPU node has no parseable 'reg'")
        elif duplicates:
            self.fail("cpus", f"duplicate hart id(s) {sorted(duplicates)}")
        else:
            self.ok("cpus", f"{len(harts)} MMU-capable hart(s): {sorted(harts)}")

        # The interrupt controller sits under each CPU node, not under /cpus.
        intc = [
            node
            for child in cpus.children
            for node in child.walk()
            if "riscv,cpu-intc" in node.compatible()
        ]
        if intc:
            self.ok("cpu-intc", "riscv,cpu-intc present under /cpus")
        else:
            self.fail("cpu-intc", "no 'riscv,cpu-intc' node under /cpus")

    def check_memory(self) -> None:
        regions = [n for n in self.root.children if n.name.startswith("memory")]
        if not regions:
            self.fail("memory", "no /memory node")
            return
        total = 0
        for node in regions:
            cells = node.cells("reg")
            if not cells or len(cells) % 4:
                self.fail("memory", f"{node.path()} has no parseable 'reg'")
                return
            for index in range(0, len(cells), 4):
                total += cells[index + 2] << 32 | cells[index + 3]
        mib = total // (1 << 20)
        if total == 0:
            self.fail("memory", "memory regions are empty")
        elif mib < 512:
            self.warn("memory", f"only {mib} MiB described")
        else:
            self.ok("memory", f"{mib} MiB in {len(regions)} region(s)")

    def check_timebase(self) -> None:
        cpus = self.root.child("cpus")
        value = cpus.cells("timebase-frequency") if cpus else None
        if value is None:
            for node in self.root.walk():
                if node.name.startswith("cpu") and node.cells("timebase-frequency"):
                    value = node.cells("timebase-frequency")
                    break
        if not value or value[0] == 0:
            self.fail("timebase", "no non-zero 'timebase-frequency'")
        else:
            self.ok("timebase", f"{value[0]} Hz")

    def resolve_chosen_path(self, text: str) -> str:
        # `stdout-path` may carry options: "serial0:115200n8".
        target = text.split(":", 1)[0]
        if target.startswith("/"):
            return target
        aliases = self.root.child("aliases")
        if aliases is not None:
            mapped = aliases.string(target)
            if mapped:
                return mapped
        return target

    def check_chosen(self) -> None:
        chosen = self.root.child("chosen")
        if chosen is None:
            self.fail("chosen", "no /chosen node (the kernel requires it)")
            return
        self.ok("chosen", "/chosen present")

        start = chosen.props.get("linux,initrd-start")
        end = chosen.props.get("linux,initrd-end")
        if start is None and end is None:
            self.warn(
                "initrd",
                "no linux,initrd-start/end; U-Boot booti must pass the initramfs",
            )
        elif start is None or end is None:
            self.fail("initrd", "only one of linux,initrd-start/end is present")
        else:
            start_value = int.from_bytes(start, "big")
            end_value = int.from_bytes(end, "big")
            if start_value >= end_value:
                self.fail("initrd", f"range [{start_value:#x}, {end_value:#x}) is empty")
            else:
                self.ok("initrd", f"[{start_value:#x}, {end_value:#x}) {end_value - start_value} bytes")

        stdout_path = chosen.string("stdout-path")
        if stdout_path is None:
            self.fail("stdout-path", "/chosen has no 'stdout-path'")
            return
        path = self.resolve_chosen_path(stdout_path)
        node = self.node_at(path)
        if node is None:
            self.fail("stdout-path", f"'{stdout_path}' resolves to {path}, which does not exist")
            return
        compatibles = node.compatible()
        matched = next((c for c in compatibles if c in UART_COMPATIBLES), None)
        if matched is None:
            self.fail(
                "stdout-path",
                f"{path} compatible={compatibles or '[]'} matches none of {UART_COMPATIBLES}",
            )
        elif not node.cells("reg"):
            self.fail("stdout-path", f"{path} has no 'reg'")
        else:
            self.ok("stdout-path", f"{path} ({matched})")

    def node_at(self, path: str) -> Node | None:
        node = self.root
        for part in [p for p in path.split("/") if p]:
            node = node.child(part)
            if node is None:
                return None
        return node

    def check_interrupts(self) -> None:
        for node in self.root.walk():
            if any(c in PLIC_COMPATIBLES for c in node.compatible()):
                cells = node.cells("reg")
                ndev = node.cells("riscv,ndev")
                detail = f"{node.path()} ({', '.join(node.compatible())})"
                if ndev:
                    detail += f" ndev={ndev[0]}"
                if not cells:
                    self.fail("plic", f"{detail} has no 'reg'")
                else:
                    self.ok("plic", detail)
                return
        self.warn("plic", f"no PLIC node ({', '.join(PLIC_COMPATIBLES)})")

    def check_devices(self) -> None:
        # MMC: the only RISC-V mmc driver in this tree.
        mmc = next((n for n in self.root.walk() if MMC_COMPATIBLE in n.compatible()), None)
        if mmc is None:
            self.warn("mmc", f"no '{MMC_COMPATIBLE}' node")
        elif mmc.cells("eswin,syscrg_csr") is None:
            self.fail("mmc", f"{mmc.path()} has no 'eswin,syscrg_csr' clock/reset handle")
        else:
            self.ok("mmc", mmc.path())

        # GMAC: needs the axi-config description the driver validates.
        gmac = next(
            (
                n
                for n in self.root.walk()
                if any(c.startswith(GMAC_COMPATIBLE_PREFIX) for c in n.compatible())
            ),
            None,
        )
        if gmac is None:
            self.warn("gmac", f"no '{GMAC_COMPATIBLE_PREFIX}*' node")
        else:
            missing = [p for p in ("snps,axi-config", "snps,blen") if p not in gmac.props]
            if missing:
                self.fail("gmac", f"{gmac.path()} misses {missing}")
            else:
                self.ok("gmac", gmac.path())

        pci = next((n for n in self.root.walk() if PCI_COMPATIBLE in n.compatible()), None)
        if pci is None:
            self.warn("pci", f"no '{PCI_COMPATIBLE}' node")
        elif not pci.cells("reg"):
            self.fail("pci", f"{pci.path()} has no 'reg'")
        else:
            self.ok("pci", pci.path())

        fb = next(
            (n for n in self.root.walk() if FRAMEBUFFER_COMPATIBLE in n.compatible()), None
        )
        if fb is None:
            self.warn("framebuffer", f"no '{FRAMEBUFFER_COMPATIBLE}' node")
        else:
            self.ok("framebuffer", fb.path())


def load_tree(path: Path) -> Node:
    blob = path.read_bytes()
    if path.suffix == ".dts" or blob[:1] == b"/":
        dtc = shutil.which("dtc")
        if dtc is None:
            raise SystemExit("dtc is required to read a .dts source")
        with tempfile.NamedTemporaryFile(suffix=".dtb", delete=True) as tmp:
            subprocess.run([dtc, "-I", "dts", "-O", "dtb", "-o", tmp.name, str(path)],
                           check=True)
            return parse_dtb(Path(tmp.name).read_bytes())
    return parse_dtb(blob)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dtb", type=Path, help="device tree (.dtb, or .dts if dtc is installed)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="print OK findings too")
    args = parser.parse_args()

    root = load_tree(args.dtb)
    checker = Checker(root)
    checker.check_cpus()
    checker.check_memory()
    checker.check_timebase()
    checker.check_chosen()
    checker.check_interrupts()
    checker.check_devices()

    failures = [finding for finding in checker.findings if finding.level == "fail"]
    warnings = [finding for finding in checker.findings if finding.level == "warn"]

    if args.json:
        print(json.dumps(
            {
                "dtb": str(args.dtb),
                "ok": not failures,
                "findings": [finding.__dict__ for finding in checker.findings],
            },
            indent=2,
        ))
    else:
        for finding in checker.findings:
            if finding.level == "ok" and not args.verbose:
                continue
            print(f"[{finding.level:4}] {finding.item:12} {finding.detail}")
        print()
        print(f"{len(checker.findings) - len(failures) - len(warnings)} ok, "
              f"{len(warnings)} warn, {len(failures)} fail")
        if failures:
            print("missing requirements the kernel parses; fix the DTB before booting", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
