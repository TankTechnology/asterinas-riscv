#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Read-only helpers for the bounded Firefox/Asterinas debug workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Iterable


_SYSCALL_NAMES = (
    "mmap",
    "munmap",
    "openat",
    "read",
    "write",
    "clone",
    "execve",
    "futex",
    "ppoll",
    "poll",
    "pipe2",
    "sendmsg",
    "recvmsg",
)
_SYSCALL_RE = re.compile(r"\b(" + "|".join(_SYSCALL_NAMES) + r")\(")
_PC_RE = re.compile(r"^pc\s+0x([0-9a-fA-F]+)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _qemu_processes() -> list[str]:
    """Return actual QEMU system/user processes without matching this probe.

    A shell-level ``pgrep -af qemu-(system|riscv64)`` also matches its own
    command line when the probe string contains that expression.  Reading
    ``/proc/*/comm`` gives us the executable name directly and keeps the
    preflight safety result authoritative.
    """
    names = {"qemu-system-riscv64", "qemu-riscv64"}
    processes: list[str] = []
    for comm_path in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = comm_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if name not in names:
            continue
        pid = comm_path.parent.name
        try:
            command_line = (comm_path.parent / "cmdline").read_bytes().replace(
                b"\0", b" "
            ).decode("utf-8", errors="replace").strip()
        except OSError:
            command_line = name
        processes.append(f"{pid} {command_line}".strip())
    return sorted(processes)


def summarize(gdb_logs: Iterable[Path], syscall_log: Path | None) -> dict[str, object]:
    """Summarize bounded debugger evidence without interpreting timing as proof."""
    combined = "\n".join(_read(path) for path in gdb_logs if path.is_file())
    counts: dict[str, int] = {name: 0 for name in _SYSCALL_NAMES}
    if syscall_log is not None and syscall_log.is_file():
        for match in _SYSCALL_RE.finditer(_read(syscall_log)):
            counts[match.group(1)] += 1
    pcs = [int(match.group(1), 16) for match in map(_PC_RE.search, combined.splitlines()) if match]
    return {
        "gdb_connected": "ASTERINAS_SYSTEM_GDB_CONNECTED" in combined
        or "GDB_ENTRY" in combined,
        "firefox_libc_start_breakpoint": "__libc_start_main@plt" in combined,
        "kernel_start_hit": "ASTERINAS_KERNEL_START_HIT" in combined,
        "pc_values": pcs,
        "syscall_counts": counts,
        "warnings": [
            line.strip()
            for line in combined.splitlines()
            if "Error while mapping" in line
            or "No debugging symbols" in line
            or "Cannot execute this command" in line
        ],
    }


def manifest(directory: Path) -> dict[str, object]:
    """Return a deterministic hash manifest for regular evidence files."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("evidence directory must be a non-symlink directory")
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {"schema": 1, "files": files}


def preflight() -> dict[str, object]:
    """Inspect local debugger tools and host safety state without mutation."""
    tools = {
        name: shutil.which(name)
        for name in ("qemu-system-riscv64", "riscv64-linux-gnu-gdb", "gdb")
    }
    binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-riscv64")
    qemu_running = _qemu_processes()
    return {
        "tools": tools,
        "binfmt_qemu_riscv64": "present" if binfmt.exists() else "absent",
        "qemu_processes": qemu_running,
        "safe_for_probe": not binfmt.exists() and not qemu_running,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    summary = subparsers.add_parser("summarize")
    summary.add_argument("--gdb", action="append", type=Path, default=[])
    summary.add_argument("--syscall-log", type=Path)
    summary.add_argument("--output", type=Path)
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("directory", type=Path)
    manifest_parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "preflight":
        value = preflight()
    elif args.command == "summarize":
        value = summarize(args.gdb, args.syscall_log)
    else:
        value = manifest(args.directory)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.command != "preflight" and args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
