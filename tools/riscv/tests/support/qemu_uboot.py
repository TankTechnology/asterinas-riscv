#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "qemu_uboot_booti.py"
PREPARE_SCRIPT = Path(__file__).resolve().parents[2] / "prepare_qemu_uboot_booti.sh"
REPO_ROOT = SCRIPT.parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
README = SCRIPT.parent / "README.md"
INITRAMFS_BUILDER = SCRIPT.parent / "make_qemu_uboot_initramfs.py"
INITRAMFS_SOURCE = SCRIPT.parent / "qemu_uboot_init.S"
sys.path.insert(0, str(SCRIPT.parent))
from megrez_contract import load_contract  # noqa: E402
import qemu_uboot_audit  # noqa: E402
import qemu_uboot_shell as shell_module  # noqa: E402
from make_qemu_uboot_initramfs import (  # noqa: E402
    InitramfsEntry,
    make_newc_archive,
)
from qemu_uboot_artifacts import (  # noqa: E402
    payload_ranges,
    validate_bdinfo_memory_layout,
)
from qemu_uboot_gate import _issue_slow_run_permit  # noqa: E402
from qemu_uboot_devices import HEADLESS, MEGREZ_BASIC, QemuDeviceSet  # noqa: E402
from qemu_ppm import PpmAudit  # noqa: E402
from qemu_uboot_variants import (  # noqa: E402
    FIRST_PROCESS_CONSOLE_LOSS,
    QemuUbootVariant,
    effective_bootargs,
    validate_registered_variant,
    variant_by_name,
)

SCRIPT_SPEC = importlib.util.spec_from_file_location("qemu_uboot_booti", SCRIPT)
assert SCRIPT_SPEC is not None
assert SCRIPT_SPEC.loader is not None
qemu_uboot_booti = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = qemu_uboot_booti
SCRIPT_SPEC.loader.exec_module(qemu_uboot_booti)
SESSION_MODULE = sys.modules["qemu_uboot_session"]
EXECUTION_MODULE = sys.modules["qemu_uboot_execution"]
EXECUTION_IO_MODULE = sys.modules["qemu_uboot_execution_io"]
RX_READY_LINE = shell_module.RX_READY_LINE
RX_INPUT_BYTES = shell_module.RX_INPUT_BYTES
RX_ACK_LINE = shell_module.RX_ACK_LINE


def valid_linux_image(size: int = 64) -> bytearray:
    image = bytearray(size)
    struct.pack_into("<I", image, 0x00, 0x0400006F)
    struct.pack_into("<Q", image, 0x08, 0x20_0000)
    struct.pack_into("<Q", image, 0x10, size)
    struct.pack_into("<I", image, 0x20, 2)
    image[0x30:0x38] = b"RISCV\0\0\0"
    struct.pack_into("<I", image, 0x38, 0x05435352)
    return image


CLEAN_SERIAL_LOG = """\
OpenSBI v1.7
Boot HART ISA Extensions    : sstc,zkr,svpbmt,svade
U-Boot 2026.07
=> version
U-Boot 2026.07
memory[0]       [0x80000000-0xffffffff], 0x80000000 bytes, flags: none
reserved[0]     [0x80000000-0x8004ffff], 0x50000 bytes, flags: no-map
reserved[1]     [0xfdea6000-0xfdeaafff], 0x5000 bytes, flags: no-notify
reserved[2]     [0xfdeab5d0-0xffffffff], 0x2154a30 bytes, flags: no-overwrite
11326096 bytes read
crc32 for 80200000 ... 80ccd28f ==> 57c40418
5048 bytes read
crc32 for 88000000 ... 880013b7 ==> 6e7844b8
=> setenv bootargs "console=ttyS0 loglevel=info init=/init"
=> printenv bootargs
bootargs=console=ttyS0 loglevel=info init=/init
=> fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"
=> fdt print /chosen
    bootargs = "console=ttyS0 loglevel=info init=/init";
3411 bytes read
crc32 for 83000000 ... 83000d52 ==> 153879f1
=> echo ASTERINAS_PRE_BOOTI
ASTERINAS_PRE_BOOTI
=> booti 0x80200000 0x83000000:${initrd_size} 0x88000000
Starting kernel ...
Enter riscv_boot
>>> Hello from RISC-V userspace on Asterinas! <<<
"""

DIAGNOSTIC_PREFIX = "ASTERINAS_FIRST_PROCESS_DIAG"
REQUIRED_DIAGNOSTIC_STAGES = (
    "diagnostic_active",
    "process_components_ready",
    "device_init_ready",
    "stdio_init_ready",
    "user_enter",
    "user_first_return",
    "user_first_syscall",
    "user_first_write_returned",
)
ALL_DIAGNOSTIC_STAGES = (
    *REQUIRED_DIAGNOSTIC_STAGES[:6],
    "user_first_page_fault",
    "user_first_page_fault_handler",
    "user_page_fault_repeated",
    *REQUIRED_DIAGNOSTIC_STAGES[6:],
)
COMPLETE_DIAGNOSTIC_LINES = (
    f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active console_registry=empty",
    f"{DIAGNOSTIC_PREFIX} stage=process_components_ready",
    f"{DIAGNOSTIC_PREFIX} stage=device_init_ready",
    f"{DIAGNOSTIC_PREFIX} stage=stdio_init_ready",
    f"{DIAGNOSTIC_PREFIX} stage=user_enter cpu=0 sepc=0x1000 sp=0x2000",
    f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_syscall sepc=0x1000",
    f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall id=64 sepc=0x1000",
    f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned fd=1 requested=50 result=50",
)
CONSOLE_LOSS_DIAGNOSTIC_LINES = (
    *COMPLETE_DIAGNOSTIC_LINES[:6],
    f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall id=56 sepc=0x1000",
    COMPLETE_DIAGNOSTIC_LINES[7],
)
PROCESS_STAGE_COMPLETE_LINE = "All components initialization in Process stage completed"
UART_REGISTRATION_LOG = (
    "\x1b[32m[     0.123]\x1b[0m "
    "\x1b[34mINFO  \x1b[0m: uart: Registered NS16550A as a console"
)

MEGREZ_BOOTARGS = "cpu_no_boost_1_6ghz loglevel=info init=/init"
MEGREZ_POSITIVE_SERIAL_LOG = f"""\
OpenSBI v1.7
Boot HART ISA Extensions    : sstc,svade
U-Boot 2026.07
=> version
U-Boot 2026.07
memory[0]       [0x80000000-0xffffffff], 0x80000000 bytes, flags: none
reserved[0]     [0x80000000-0x8004ffff], 0x50000 bytes, flags: no-map
reserved[1]     [0xfdea6000-0xfdeaafff], 0x5000 bytes, flags: no-notify
reserved[2]     [0xfdeab5d0-0xffffffff], 0x2154a30 bytes, flags: no-overwrite
11326096 bytes read
crc32 for 80200000 ... 80ccd28f ==> 57c40418
5048 bytes read
crc32 for 88000000 ... 880013b7 ==> 6e7844b8
=> setenv bootargs "{MEGREZ_BOOTARGS}"
=> printenv bootargs
bootargs={MEGREZ_BOOTARGS}
=> fdt set /chosen bootargs "{MEGREZ_BOOTARGS}"
=> if fdt get value aster_rng_seed /chosen rng-seed; then fdt rm /chosen rng-seed; fi
libfdt fdt_getprop(): FDT_ERR_NOTFOUND
=> fdt print /chosen
    bootargs = "{MEGREZ_BOOTARGS}";
3411 bytes read
crc32 for 83000000 ... 83000d52 ==> 153879f1
=> echo ASTERINAS_PRE_BOOTI
ASTERINAS_PRE_BOOTI
=> booti 0x80200000 0x83000000:${{initrd_size}} 0x88000000
Starting kernel ...
Enter riscv_boot
Booting 3 processors
Processor 2 started. Spinning for tasks.
Processor 3 started. Spinning for tasks.
Processor 1 started. Spinning for tasks.
All application processors started. The BSP continues to run.
OSTD initialized. Preparing components.
use randomness based on the timestamp, which is insecure
[kernel] rootfs is ready
>>> Hello from RISC-V userspace on Asterinas! <<<
"""
MEGREZ_STALE_SERIAL_LOG = (
    MEGREZ_POSITIVE_SERIAL_LOG.replace(
        f'=> setenv bootargs "{MEGREZ_BOOTARGS}"',
        '=> setenv bootargs "cpu_no_boost_1_6ghz"',
    )
    .replace(
        f"bootargs={MEGREZ_BOOTARGS}",
        "bootargs=cpu_no_boost_1_6ghz",
        1,
    )
    .replace(
        ">>> Hello from RISC-V userspace on Asterinas! <<<",
        "[     0.668] ERROR : Uncaught panic:\n"
        "\tFailed to run the init process: Error { errno: ENOENT, "
        'msg: Some("found a negative dentry") }',
    )
)

__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
