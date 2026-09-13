# SPDX-License-Identifier: MPL-2.0

"""Add an existing RISC-V BusyBox and its small shared-library closure."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import struct
import subprocess


APPLETS = "sh cat ls dmesg uname mount umount reboot sync sleep ps mkdir echo pwd free hexdump".split()


def elf_info(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(20)
    if (
        header[:6] != b"\x7fELF\x02\x01"
        or len(header) != 20
        or struct.unpack_from("<H", header, 18)[0] != 243
    ):
        raise ValueError(f"not a little-endian RISC-V ELF64 file: {path}")
    return subprocess.check_output(["readelf", "-l", "-d", str(path)], text=True)


def install_busybox(stage: Path, busybox: Path) -> None:
    information = elf_info(busybox)
    interpreter = re.search(r"Requesting program interpreter: ([^\]]+)\]", information)
    pending = []
    search = []
    if interpreter:
        loader = Path(interpreter[1])
        if not loader.is_absolute() or ".." in loader.parts:
            raise ValueError("unsafe BusyBox interpreter path")
        pending.append((loader, loader))
        search.append(loader.parent)
    # The cached Nix BusyBox and its libc use absolute RUNPATH entries. Refuse
    # unresolved dependencies instead of making a boot-time loader failure.
    for match in re.finditer(r"\((?:RUNPATH|RPATH)\).*\[([^\]]+)\]", information):
        for value in match[1].split(":"):
            directory = Path(value)
            if not directory.is_absolute() or ".." in directory.parts:
                raise ValueError("BusyBox requires an unsupported library search path")
            search.append(directory)
    pending.append((busybox, Path("/bin/busybox")))
    installed = set()
    while pending:
        source, destination = pending.pop()
        if destination in installed:
            continue
        details = elf_info(source)
        target = stage / destination.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755)
        installed.add(destination)
        for name in re.findall(r"\(NEEDED\).*\[([^\]]+)\]", details):
            if Path(name).name != name:
                raise ValueError("unsafe shared library name")
            candidates = [
                directory / name for directory in search if (directory / name).is_file()
            ]
            if not candidates:
                raise ValueError(f"unresolved BusyBox library: {name}")
            pending.append((candidates[0], candidates[0]))
    for applet in APPLETS:
        (stage / "bin" / applet).symlink_to("busybox")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, required=True)
    args = parser.parse_args()
    install_busybox(args.stage, args.busybox)
