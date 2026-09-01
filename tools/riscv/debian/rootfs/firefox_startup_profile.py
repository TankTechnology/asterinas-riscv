#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Collect one bounded Firefox startup transcript without running the web gate.

This intentionally reuses the frozen browser-web QEMU artifact contract, but
stops after the ordered desktop/Firefox startup markers.  It is for profiling,
not a pass/fail browser-quality gate.
"""

from __future__ import annotations

import argparse
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
from tools.riscv.debian.rootfs.desktop_m3_gate import _BOCHS_BAR_RE
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output


_MARKERS = (
    ("basic", b"A_WEB_TIMELINE marker=BOOT_BASIC_TARGET"),
    ("x-socket-ready", b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready"),
    ("firefox-exec", b"ASTERINAS_FIREFOX_WEB_EXEC"),
    ("marionette", b"BOOT_MARIONETTE_PORT_READY"),
)

# U-Boot's console command buffer is smaller than the bootargs accepted by the
# kernel.  Keep each command comfortably below the observed 1 KiB boundary;
# long diagnostic combinations are written through two environment variables.
_UBOOT_COMMAND_SAFE_LIMIT = 700


def _profile_boot_commands(operations: BrowserWebQemuOperations,
                           framebuffer_address: int) -> tuple[str, ...]:
    """Return framebuffer boot commands without overflowing U-Boot input."""

    commands = list(operations._boot_commands(framebuffer_address))
    if not commands or len(commands[-1]) <= _UBOOT_COMMAND_SAFE_LIMIT:
        return tuple(commands)
    bootargs = operations.BOOTARGS
    chunks: list[str] = []
    offset = 0
    while offset < len(bootargs):
        # Leave room for the setenv syntax and a closing quote.  The index is
        # deliberately short so this remains well below U-Boot's parser limit.
        prefix_len = len(f'setenv ast_bootargs_{len(chunks)} ""')
        payload_len = _UBOOT_COMMAND_SAFE_LIMIT - prefix_len
        if payload_len <= 0:
            raise GateFailure("invalid U-Boot command safety limit")
        chunks.append(bootargs[offset : offset + payload_len])
        offset += payload_len
    assignments = [
        f'setenv ast_bootargs_{index} "{chunk}"'
        for index, chunk in enumerate(chunks)
    ]
    expansion = "".join(f"${{ast_bootargs_{index}}}" for index in range(len(chunks)))
    commands[-1:] = [*assignments, f'setenv bootargs "{expansion}"']
    if any(len(command) > _UBOOT_COMMAND_SAFE_LIMIT for command in commands):
        raise GateFailure("diagnostic bootargs still exceed U-Boot command buffer")
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
) -> int:
    _safe_output(config.output_directory)
    diagnostic_args = (
        " systemd.setenv=ASTERINAS_FIREFOX_PS_DIAGNOSTIC=1"
        " systemd.setenv=ASTERINAS_FIREFOX_PROC_DIAGNOSTIC=1"
        if process_diagnostic
        else ""
    )
    if epoll_entry_diagnostic:
        diagnostic_args += (
            " asterinas.epoll_profile=1"
            " asterinas.epoll_entry_profile=1"
        )
    if timerfd_diagnostic:
        diagnostic_args += " asterinas.timerfd_profile=1"
    if syscall_diagnostic:
        diagnostic_args += " asterinas.syscall_profile=1"
    if pagecache_diagnostic:
        diagnostic_args += " asterinas.vm_pagecache_profile=1"
    if read_detail_diagnostic:
        diagnostic_args += " asterinas.read_detail_profile=1"
    if futex_diagnostic:
        diagnostic_args += " asterinas.futex_profile=1"
    BrowserWebQemuOperations.BOOTARGS = BrowserWebQemuOperations.BOOTARGS.replace(
        " -- --root-init=systemd",
        f" asterinas.vm_profile=1{diagnostic_args} -- --root-init=systemd",
    )
    operations = BrowserWebQemuOperations(config)
    operations.__enter__()
    session = None
    started = time.monotonic()
    transcript = b""
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
        for name, marker in _MARKERS:
            try:
                serial.wait_for(marker, deadline)
                print(
                    f"STARTUP_PROFILE_MARKER name={name} "
                    f"elapsed={time.monotonic() - started:.3f}",
                    flush=True,
                )
            except BaseException as error:
                print(
                    f"STARTUP_PROFILE_MISSING name={name} "
                    f"elapsed={time.monotonic() - started:.3f} error={error}",
                    flush=True,
                )
                break
        transcript = serial.transcript
        (config.output_directory / "startup.serial.log").write_bytes(transcript)
        print(
            f"STARTUP_PROFILE_DONE elapsed={time.monotonic() - started:.3f} "
            f"bytes={len(transcript)}",
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
