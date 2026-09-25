#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Collect one bounded Firefox startup transcript without driving the web gate.

This intentionally reuses the frozen browser-web QEMU artifact contract, but
stops after the desktop/Firefox startup markers. It is for profiling,
not a pass/fail browser-quality gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Support both `python -m ...` and the documented direct script invocation.
# In the latter case Python puts `tools/riscv/debian/rootfs` on sys.path rather
# than the repository root, so the sibling `tools` package would otherwise be
# invisible unless callers remembered to export PYTHONPATH.
if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parents[4]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from tools.riscv.debian.rootfs.browser_web_qemu_gate import BrowserWebQemuOperations
from tools.riscv.debian.rootfs.desktop_m5_network_gate import NetworkMode
from tools.riscv.debian.rootfs.desktop_m3_gate import (
    _BOCHS_BAR_RE,
    _UBOOT_COMMAND_SAFE_LIMIT,
    _bounded_uboot_bootargs_commands,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output


_MARKERS = (
    ("x-socket-ready", b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready"),
    ("firefox-exec", b"ASTERINAS_FIREFOX_WEB_EXEC"),
    ("marionette", b"Marionette\tINFO\tListening on port 2828"),
)


def _wait_for_marker_line(
    serial, marker: bytes, deadline: float, *, start: int = 0
) -> bytes:
    """Wait until a marker's whole newline-terminated evidence record arrives."""

    transcript = serial.wait_for(marker, deadline, start=start)
    marker_start = transcript.find(marker, start)
    if marker_start < 0:
        raise GateFailure("startup marker wait returned inconsistent evidence")
    return serial.wait_for(b"\n", deadline, start=marker_start + len(marker))


def _capture_startup_markers(
    serial, deadline: float, started: float
) -> list[dict[str, object]]:
    """Capture startup events even when concurrent services log out of order."""

    records: list[dict[str, object]] = []
    pending = dict(_MARKERS)
    while pending:
        try:
            marker = serial.wait_for_any(tuple(pending.values()), deadline)
        except TimeoutError as error:
            missing = ", ".join(repr(value) for value in pending.values())
            raise TimeoutError(f"startup markers not seen: {missing}") from error
        name = next(name for name, value in pending.items() if value == marker)
        complete = _wait_for_marker_line(serial, marker, deadline)
        marker_start = complete.find(marker)
        line_end = complete.find(b"\n", marker_start + len(marker))
        elapsed = time.monotonic() - started
        records.append(
            {
                "name": name,
                "host_elapsed_seconds": round(elapsed, 3),
                "evidence": complete[marker_start:line_end].decode("ascii", "replace"),
            }
        )
        del pending[name]
        print(
            f"STARTUP_PROFILE_MARKER name={name} elapsed={elapsed:.3f}",
            flush=True,
        )
    return records


def _write_profile_result(
    path: Path,
    markers: list[dict[str, object]],
    transcript: bytes,
    total_seconds: float,
) -> None:
    """Publish a bounded, exclusive summary next to the raw serial evidence."""

    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "markers": markers,
                "serial_sha256": hashlib.sha256(transcript).hexdigest(),
                "total_seconds": round(total_seconds, 3),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if len(payload) > 16 * 1024:
        raise GateFailure("startup profile result exceeds its size bound")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        cursor = 0
        while cursor < len(payload):
            written = os.write(descriptor, payload[cursor:])
            if written <= 0:
                raise GateFailure("startup profile result write did not advance")
            cursor += written
    finally:
        os.close(descriptor)


def _profile_boot_commands(
    operations: BrowserWebQemuOperations, framebuffer_address: int
) -> tuple[str, ...]:
    """Return framebuffer boot commands without overflowing U-Boot input."""

    commands = list(operations._boot_commands(framebuffer_address))
    if not commands or all(
        len(command.encode()) <= _UBOOT_COMMAND_SAFE_LIMIT for command in commands
    ):
        return tuple(commands)
    direct = f'setenv bootargs "{operations.BOOTARGS}"'
    if commands[-1] != direct:
        raise GateFailure("unexpected unbounded U-Boot command")
    commands[-1:] = _bounded_uboot_bootargs_commands(operations.BOOTARGS)
    return tuple(commands)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "kernel",
        "uboot",
        "dtb",
        "stage1-initramfs",
        "root-image",
        "root-manifest",
        "packages-lock",
        "package-checksums",
        "output-directory",
    ):
        parser.add_argument(f"--{option}", required=True, type=Path)
    parser.add_argument("--boot-timeout", type=float, default=360.0)
    parser.add_argument("--smp", type=int, choices=(4,), default=4)
    parser.add_argument(
        "--firefox-process-diagnostic",
        action="store_true",
        help="enable bounded Firefox ps/proc snapshots after exec",
    )
    parser.add_argument(
        "--epoll-entry-diagnostic",
        action="store_true",
        help="enable caller/fd-level epoll entry sampling",
    )
    parser.add_argument(
        "--timerfd-diagnostic",
        action="store_true",
        help="enable aggregate timerfd set/read/expiry sampling",
    )
    parser.add_argument(
        "--syscall-diagnostic",
        action="store_true",
        help="enable aggregate common-syscall entry/completion sampling",
    )
    parser.add_argument(
        "--pagecache-diagnostic",
        action="store_true",
        help="enable aggregate synchronous page-cache backend-read sampling",
    )
    parser.add_argument(
        "--read-detail-diagnostic",
        action="store_true",
        help="log bounded slow read fd/type/path samples",
    )
    parser.add_argument(
        "--futex-diagnostic",
        action="store_true",
        help="log bounded slow futex wait identity and wake/timeout outcome",
    )
    parser.add_argument(
        "--local-icache-diagnostic",
        action="store_true",
        help=(
            "diagnose global RISC-V instruction-cache synchronization cost; "
            "not an SMP correctness or acceptance mode"
        ),
    )
    return parser


def _config(args: argparse.Namespace) -> GateConfig:
    if args.boot_timeout <= 0:
        raise ValueError("boot timeout must be positive")
    return GateConfig(
        args.kernel,
        args.uboot,
        args.dtb,
        args.stage1_initramfs,
        args.root_image,
        args.root_manifest,
        args.packages_lock,
        args.package_checksums,
        args.output_directory,
        args.smp,
        args.boot_timeout,
        30.0,
        15.0,
    )


def _diagnostic_kernel_args(
    *,
    process_diagnostic: bool = False,
    epoll_entry_diagnostic: bool = False,
    timerfd_diagnostic: bool = False,
    syscall_diagnostic: bool = False,
    pagecache_diagnostic: bool = False,
    read_detail_diagnostic: bool = False,
    futex_diagnostic: bool = False,
    local_icache_diagnostic: bool = False,
) -> str:
    """Build opt-in profiling arguments without changing the normal gate."""

    args = "asterinas.vm_profile=1"
    if process_diagnostic:
        args += (
            " systemd.setenv=ASTERINAS_FIREFOX_PS_DIAGNOSTIC=1"
            " systemd.setenv=ASTERINAS_FIREFOX_PROC_DIAGNOSTIC=1"
        )
    if epoll_entry_diagnostic:
        args += " asterinas.epoll_profile=1 asterinas.epoll_entry_profile=1"
    if timerfd_diagnostic:
        args += " asterinas.timerfd_profile=1"
    if syscall_diagnostic:
        args += " asterinas.syscall_profile=1"
    if pagecache_diagnostic:
        args += " asterinas.vm_pagecache_profile=1"
    if read_detail_diagnostic:
        args += " asterinas.read_detail_profile=1"
    if futex_diagnostic:
        args += " asterinas.futex_profile=1"
    if local_icache_diagnostic:
        args += " asterinas.vm_local_icache=1"
    return args


def run(
    config: GateConfig,
    *,
    process_diagnostic: bool = False,
    epoll_entry_diagnostic: bool = False,
    timerfd_diagnostic: bool = False,
    syscall_diagnostic: bool = False,
    pagecache_diagnostic: bool = False,
    read_detail_diagnostic: bool = False,
    futex_diagnostic: bool = False,
    local_icache_diagnostic: bool = False,
) -> int:
    _safe_output(config.output_directory)
    diagnostic_args = _diagnostic_kernel_args(
        process_diagnostic=process_diagnostic,
        epoll_entry_diagnostic=epoll_entry_diagnostic,
        timerfd_diagnostic=timerfd_diagnostic,
        syscall_diagnostic=syscall_diagnostic,
        pagecache_diagnostic=pagecache_diagnostic,
        read_detail_diagnostic=read_detail_diagnostic,
        futex_diagnostic=futex_diagnostic,
        local_icache_diagnostic=local_icache_diagnostic,
    )
    operations = BrowserWebQemuOperations(config, network_mode=NetworkMode.DIRECT)
    operations.BOOTARGS = operations.BOOTARGS.replace(
        " -- --root-init=systemd",
        f" {diagnostic_args} -- --root-init=systemd",
    )
    operations.__enter__()
    session = None
    started = time.monotonic()
    transcript = b""
    marker_records: list[dict[str, object]] = []
    try:
        operations.invalidate(config)
        snapshots = operations.snapshot_inputs(config)
        identity = operations.validate_inputs(config, snapshots)
        prepared = operations.prepare(config, snapshots, identity)
        session = operations.launch(config, prepared)
        serial = session["serial"]
        deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(b"=> ", deadline)
        operations._send_uboot(session, "pci enum", 1, deadline)
        bar_start = serial.checkpoint()
        operations._send_uboot(session, "pci bar 0.1.0", 2, deadline)
        match = _BOCHS_BAR_RE.search(serial.transcript[bar_start:])
        if match is None:
            raise GateFailure("failed to discover bochs framebuffer BAR0")
        framebuffer_address = int(match.group(1), 16)
        for index, command in enumerate(
            _profile_boot_commands(operations, framebuffer_address), 3
        ):
            operations._send_uboot(session, command, index, deadline)
        marker = "__ASTERINAS_STARTUP_PROFILE_BOOT__"
        serial.send(
            (
                f"echo {marker}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x90000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)
        marker_records = _capture_startup_markers(serial, deadline, started)
        transcript = serial.transcript
        (config.output_directory / "startup.serial.log").write_bytes(transcript)
        elapsed = time.monotonic() - started
        _write_profile_result(
            config.output_directory / "startup-profile.json",
            marker_records,
            transcript,
            elapsed,
        )
        print(
            f"STARTUP_PROFILE_DONE elapsed={elapsed:.3f} bytes={len(transcript)}",
            flush=True,
        )
        return 0
    except BaseException as error:
        if session is not None:
            transcript = session["serial"].transcript
            (config.output_directory / "startup.serial.log").write_bytes(transcript)
        print(
            f"STARTUP_PROFILE_ERROR elapsed={time.monotonic() - started:.3f} "
            f"type={type(error).__name__} error={error}",
            flush=True,
        )
        return 1
    finally:
        if session is not None:
            try:
                session["monitor"].command("quit", time.monotonic() + 5)
            except BaseException:
                pass
            try:
                operations.close_monitor(session)
            except BaseException:
                pass
            try:
                operations.cleanup_process(session, config)
            except BaseException:
                pass
            try:
                operations.drain_serial(session, config)
            except BaseException:
                pass
        try:
            operations._require_output().invalidate("boot.ext4", "debian-root.run.ext2")
        except BaseException:
            pass
        operations.close()


def main() -> int:
    args = _parser().parse_args()
    return run(
        _config(args),
        process_diagnostic=args.firefox_process_diagnostic,
        epoll_entry_diagnostic=args.epoll_entry_diagnostic,
        timerfd_diagnostic=args.timerfd_diagnostic,
        syscall_diagnostic=args.syscall_diagnostic,
        pagecache_diagnostic=args.pagecache_diagnostic,
        read_detail_diagnostic=args.read_detail_diagnostic,
        futex_diagnostic=args.futex_diagnostic,
        local_icache_diagnostic=args.local_icache_diagnostic,
    )


if __name__ == "__main__":
    raise SystemExit(main())
