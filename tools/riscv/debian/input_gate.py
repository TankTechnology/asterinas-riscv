#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Define the host-side protocol for the Debian RISC-V input gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


READY_MARKER = b"__DEBIAN_INPUT_GATE_READY__"
PASS_MARKER = b"__DEBIAN_INPUT_GATE_PASS__"
KEY_SEQUENCE = ("a", "shift-b", "backspace", "ctrl-c")
PANIC_MARKERS = (b"Kernel panic", b"kernel panic", b"BUG:", b"panic!")


@dataclass(frozen=True)
class GateResult:
    """Summarize marker evidence found in a gate transcript."""

    ready: bool
    complete: bool
    panics: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Report whether the transcript proves successful completion."""

        return self.ready and self.complete and not self.panics


def qemu_argv(
    uboot: Path,
    boot_disk: Path,
    monitor_socket: Path,
    smp: int = 4,
) -> list[str]:
    """Build the deterministic, network-isolated QEMU command line."""

    if isinstance(smp, bool) or not isinstance(smp, int) or smp <= 0:
        raise ValueError("SMP must be a strictly positive integer")

    return [
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-m",
        "2G",
        "-smp",
        str(smp),
        "-no-reboot",
        "-kernel",
        str(uboot),
        "-drive",
        f"if=none,format=raw,file={boot_disk},id=bootdisk",
        "-device",
        "virtio-blk-device,drive=bootdisk",
        "-device",
        "virtio-tablet-device",
        "-device",
        "virtio-keyboard-device",
        "-display",
        "none",
        "-monitor",
        f"unix:{monitor_socket},server=on,wait=off",
        "-serial",
        "stdio",
        "-nic",
        "none",
    ]


def classify_transcript(transcript: bytes) -> GateResult:
    """Classify readiness, completion, and panic evidence in a transcript."""

    panics = tuple(
        marker.decode("ascii") for marker in PANIC_MARKERS if marker in transcript
    )
    return GateResult(
        ready=READY_MARKER in transcript,
        complete=PASS_MARKER in transcript,
        panics=panics,
    )
