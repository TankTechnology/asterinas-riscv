#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

"""Build the minimal deterministic initramfs used by the QEMU U-Boot test."""

from __future__ import annotations

import argparse
import gzip
import os
import stat
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import qemu_uboot_secure_io


SOURCE = Path(__file__).with_name("qemu_uboot_init.S")
COMPILER = "riscv64-linux-gnu-gcc"
ELF_MACHINE_RISCV = 243


@dataclass(frozen=True)
class InitramfsEntry:
    """One additional deterministic newc archive entry."""

    name: str
    data: bytes
    mode: int


def _validate_extra_entries(extra_entries: Sequence[InitramfsEntry]) -> None:
    names = {"dev", "proc", "sys", "tmp", "init"}
    for entry in extra_entries:
        if entry.name == "TRAILER!!!":
            raise ValueError("reserved initramfs entry name: TRAILER!!!")
        parts = entry.name.split("/")
        if (
            not entry.name
            or entry.name.startswith("/")
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("initramfs entry name must be a canonical relative path")
        try:
            entry.name.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("initramfs entry name must be ASCII") from error
        if entry.name in names:
            raise ValueError(f"duplicate initramfs entry: {entry.name}")
        names.add(entry.name)


def _align4(data: bytes) -> bytes:
    return data + bytes((-len(data)) % 4)


def _newc_entry(
    name: str,
    data: bytes,
    mode: int,
    inode: int,
    link_count: int = 1,
) -> bytes:
    name_bytes = name.encode("ascii") + b"\0"
    fields = (
        inode,
        mode,
        0,
        0,
        link_count,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(name_bytes),
        0,
    )
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
    return _align4(header + name_bytes) + _align4(data)


def make_newc_archive(
    init_elf: bytes,
    *,
    extra_entries: Sequence[InitramfsEntry] = (),
) -> bytes:
    _validate_extra_entries(extra_entries)
    base_entries: tuple[tuple[str, bytes, int, int], ...] = (
        ("dev", b"", stat.S_IFDIR | 0o755, 1),
        ("proc", b"", stat.S_IFDIR | 0o755, 2),
        ("sys", b"", stat.S_IFDIR | 0o755, 3),
        ("tmp", b"", stat.S_IFDIR | 0o1777, 4),
        ("init", init_elf, stat.S_IFREG | 0o755, 5),
    )
    entries: Iterable[tuple[str, bytes, int, int]] = (
        *base_entries,
        *(
            (entry.name, entry.data, entry.mode, inode)
            for inode, entry in enumerate(extra_entries, start=len(base_entries) + 1)
        ),
    )
    archive = b"".join(
        _newc_entry(name, data, mode, inode) for name, data, mode, inode in entries
    )
    trailer_inode = len(base_entries) + len(extra_entries) + 1
    archive += _newc_entry("TRAILER!!!", b"", 0, trailer_inode)
    return archive + bytes((-len(archive)) % 512)


def _validate_riscv_elf(image: bytes) -> None:
    if len(image) < 20 or image[:5] != b"\x7fELF\x02":
        raise ValueError("compiler did not produce a 64-bit ELF executable")
    machine = struct.unpack_from("<H", image, 18)[0]
    if machine != ELF_MACHINE_RISCV:
        raise ValueError(f"compiler produced ELF machine {machine}, expected RISC-V")


def _compile_init(
    output: Path,
    *,
    source: Path = SOURCE,
    cpp_defines: Sequence[str] = (),
) -> None:
    subprocess.run(
        [
            COMPILER,
            "-march=rv64gc",
            "-mabi=lp64d",
            "-nostdlib",
            "-static",
            "-s",
            "-Wl,--build-id=none",
            "-Wl,-z,noexecstack",
            *(f"-D{definition}" for definition in cpp_defines),
            "-o",
            str(output),
            str(source),
        ],
        check=True,
    )


def _write_output_atomic(output: Path, payload: bytes) -> None:
    output_name = output.name
    if not output_name or output_name in (".", ".."):
        raise ValueError("initramfs output must name a file")

    output_parent = Path(os.path.abspath(output.parent))
    with qemu_uboot_secure_io.PinnedOutputDirectory.open(output_parent) as directory:
        existing = directory.entry_metadata(output_name)
        if existing is not None:
            mode = existing.st_mode
            if stat.S_ISLNK(mode):
                raise ValueError("initramfs output must not be a symbolic link")
            if not stat.S_ISREG(mode):
                raise ValueError("initramfs output must be a regular file")

        with directory.atomic_write(output_name, payload) as publication:
            directory.verify_current()
            directory.verify_entry(output_name, publication.identity)


def build_initramfs(
    output: Path,
    *,
    source: Path = SOURCE,
    cpp_defines: Sequence[str] = (),
    extra_entries: Sequence[InitramfsEntry] = (),
) -> None:
    with tempfile.TemporaryDirectory(prefix="qemu-uboot-init-") as tmp:
        init_path = Path(tmp) / "init"
        _compile_init(init_path, source=source, cpp_defines=cpp_defines)
        init_elf = init_path.read_bytes()
        _validate_riscv_elf(init_elf)
        archive = make_newc_archive(init_elf, extra_entries=extra_entries)
        compressed = gzip.compress(archive, mtime=0)

    _write_output_atomic(output, compressed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="output .cpio.gz path")
    parser.add_argument(
        "--init-elf",
        type=Path,
        default=None,
        help="pack a prebuilt /init ELF instead of compiling qemu_uboot_init.S",
    )
    args = parser.parse_args()
    if args.init_elf is not None:
        init_elf = args.init_elf.read_bytes()
        _validate_riscv_elf(init_elf)
        archive = make_newc_archive(init_elf)
        compressed = gzip.compress(archive, mtime=0)
        _write_output_atomic(args.output, compressed)
    else:
        build_initramfs(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
