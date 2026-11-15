#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fill (or inspect) the kernel's symbol table.

The kernel reserves a fixed region for its own symbol table, because the table
can only be known once the image has been linked:

    kernel/src/trace/kallsyms.rs:  BLOB = magic "ASTERKALLSYMSv1\\0" + 2 MiB

This tool reads the symbols out of the linked ELF, packs them -- sorted by
address, names deduplicated and truncated -- and writes the result into that
region of the artifact(s) it is given. The same bytes live in the ELF and in
the flat `Image` that boots, so both are patched from the magic search alone.

    tools/riscv/ksym/kallsyms.py patch <elf> <image> [<image> ...]
    tools/riscv/ksym/kallsyms.py show <image> [--limit N] [--grep PATTERN]
    tools/riscv/ksym/kallsyms.py lookup <image> <addr> [<addr> ...]

The table layout is documented in `kernel/src/trace/kallsyms.rs`; the two must
stay in sync (`BLOB_SIZE`, `MAGIC`, `VERSION`).

Needs `llvm-nm` (it demangles Rust's v0 names; GNU nm does not).
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys

MAGIC = b"ASTERKALLSYMSv1\x00"
BLOB_SIZE = 2 * 1024 * 1024
VERSION = 1
HEADER_LEN = 32
NAME_TRUNC = 72

HEADER = struct.Struct("<IIIIIIQ")  # version, count, table_off, names_off, names_len, pad, base
ENTRY = struct.Struct("<II")  # address offset, name offset
# 0 version | 4 count | 8 table_off | 12 names_off | 16 names_len | 20 pad | 24 addr_base


def load_symbols(elf: str) -> list[tuple[int, str]]:
    """Reads (address, demangled name) for every function symbol in `elf`."""

    try:
        out = subprocess.run(
            ["llvm-nm", "-C", "--defined-only", "--numeric-sort", elf],
            capture_output=True, text=True, check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("llvm-nm not found (Debian: llvm, Arch: llvm)")
    except subprocess.CalledProcessError as err:
        sys.exit(f"llvm-nm failed: {err.stderr.strip()}")

    symbols = []
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        address, kind, name = parts
        # Lowercase is local, uppercase global; both are text.
        if kind not in ("t", "T"):
            continue
        try:
            address = int(address, 16)
        except ValueError:
            continue
        # The image is linked into the high half but the early boot code is
        # also reachable at its physical address; the kernel normalizes both
        # into the virtual space before looking a symbol up, so store that
        # form. It also keeps the table's 32-bit offsets in range.
        if 0x8000_0000 <= address < 0x9000_0000:
            address |= 0xFFFF_FFFF_0000_0000
        # Linker-defined absolutes (`__executable_start` is 0) are not code;
        # they would also push the table's 32-bit offsets out of range.
        if address < 0xFFFF_FFFF_8000_0000:
            continue
        # Compiler-local labels (`LLVM` names them `.L...`) sit inside
        # functions; they are not entries, and a lookup that answers with one
        # hides the function it belongs to.
        if name.startswith(".L"):
            continue
        symbols.append((address, name))

    if not symbols:
        sys.exit(f"{elf}: no text symbols (a stripped image?)")
    symbols.sort(key=lambda item: item[0])
    return symbols


def build_blob(symbols: list[tuple[int, str]]) -> tuple[bytes, dict[str, int]]:
    """Packs the table the way the kernel parses it."""

    base = symbols[0][0]
    names: dict[str, int] = {}
    name_blob = bytearray()
    table = bytearray()
    count = 0
    # Keep any symbol whose size (the earlier one) was well below the truncation
    # limit readable in `show`, so no surrogate pairs or split characters leak.
    for address, name in symbols:
        encoded = name.encode("utf-8")
        if len(encoded) > NAME_TRUNC:
            encoded = encoded[:NAME_TRUNC].decode("utf-8", "ignore").encode("utf-8")
        offset = names.get(encoded.decode("utf-8", "replace"))
        if offset is None:
            offset = len(name_blob)
            name_blob += encoded + b"\x00"
            names[encoded.decode("utf-8", "replace")] = offset
        if address < base:
            base = address
        table += ENTRY.pack(address - base, offset)
        count += 1

    table_off = HEADER_LEN
    names_off = table_off + len(table)
    while names_off % 8:
        names_off += 1
    pad = names_off - (table_off + len(table))

    header = HEADER.pack(VERSION, count, table_off, names_off, len(name_blob), 0, base)
    blob = header + table + b"\x00" * pad + name_blob
    stats = {
        "symbols": count,
        "names": len(names),
        "table": len(table),
        "name_bytes": len(name_blob),
        "total": len(blob),
    }
    if len(blob) > BLOB_SIZE:
        sys.exit(
            f"symbol table needs {len(blob)} bytes, the reserved region is "
            f"{BLOB_SIZE} (raise BLOB_SIZE in kernel/src/trace/kallsyms.rs)"
        )
    return blob, stats


def patch(path: str, blob: bytes) -> int:
    data = bytearray(open(path, "rb").read())
    at = data.find(MAGIC)
    if at < 0:
        sys.exit(f"{path}: placeholder not found (built without kernel/src/trace/kallsyms.rs?)")
    if data.find(MAGIC, at + 1) >= 0:
        sys.exit(f"{path}: placeholder appears more than once")
    start = at + len(MAGIC)
    end = start + BLOB_SIZE
    if end > len(data):
        sys.exit(f"{path}: file ends inside the reserved region")
    data[start:end] = blob + b"\x00" * (BLOB_SIZE - len(blob))
    with open(path, "wb") as handle:
        handle.write(data)
    return at


def read_table(path: str) -> tuple[int, list[tuple[int, str]]]:
    data = open(path, "rb").read()
    at = data.find(MAGIC)
    if at < 0:
        sys.exit(f"{path}: placeholder not found")
    base = at + len(MAGIC)
    version, count, table_off, names_off, names_len, _, addr_base = HEADER.unpack_from(data, base)
    if version != VERSION:
        sys.exit(f"{path}: table version {version}, this tool writes {VERSION}")
    if table_off + count * ENTRY.size > BLOB_SIZE or names_off + names_len > BLOB_SIZE:
        sys.exit(f"{path}: table header points outside the reserved region")
    symbols = []
    for index in range(count):
        offset, name_off = ENTRY.unpack_from(data, base + table_off + index * ENTRY.size)
        name_at = base + names_off + name_off
        name_end = data.find(b"\x00", name_at)
        name = data[name_at:name_end].decode("utf-8", "replace")
        symbols.append((addr_base + offset, name))
    return base, symbols


def cmd_patch(args) -> int:
    symbols = load_symbols(args.elf)
    blob, stats = build_blob(symbols)
    print(f"symbols={stats['symbols']} unique_names={stats['names']} "
          f"table={stats['table']}B names={stats['name_bytes']}B total={stats['total']}B")
    for path in args.artifacts:
        at = patch(path, blob)
        print(f"patched {path} at offset 0x{at:x}")
    return 0


def cmd_show(args) -> int:
    _, symbols = read_table(args.image)
    shown = 0
    for address, name in symbols:
        if args.grep and args.grep not in name:
            continue
        print(f"{address:016x} t {name}")
        shown += 1
        if args.limit and shown >= args.limit:
            break
    return 0


def cmd_lookup(args) -> int:
    _, symbols = read_table(args.image)
    addrs = [int(value, 0) for value in args.addresses]
    for addr in addrs:
        # Same normalization as the kernel: a physical image address is the low
        # half of the virtual one.
        target = addr
        if 0x8000_0000 <= addr < 0x9000_0000:
            target = addr | 0xFFFF_FFFF_0000_0000
        found = None
        for address, name in symbols:
            if address <= target:
                found = (address, name)
            else:
                break
        if found:
            print(f"{addr:#018x} -> {found[1]} (+{target - found[0]:#x})")
        else:
            print(f"{addr:#018x} -> ?")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("patch", help="write the table into an image")
    p.add_argument("elf")
    p.add_argument("artifacts", nargs="+")
    p.set_defaults(func=cmd_patch)

    s = sub.add_parser("show", help="print the table from a patched image")
    s.add_argument("image")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--grep")
    s.set_defaults(func=cmd_show)

    l = sub.add_parser("lookup", help="resolve addresses in a patched image")
    l.add_argument("image")
    l.add_argument("addresses", nargs="+")
    l.set_defaults(func=cmd_lookup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
