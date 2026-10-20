#!/usr/bin/env python3
"""Run the bounded futex probe as the only Asterinas RISC-V init."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000


class SerialBoot:
    def __init__(self, argv: list[str], log: Path) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = log.open("wb")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert self.proc.stdout is not None and self.proc.stdin is not None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.stdin = self.proc.stdin
        self.pending = bytearray()
        self.transcript = bytearray()

    def read_until(self, marker: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while marker not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"waiting for {marker!r}; tail={self.transcript[-1000:]!r}")
            for key, _ in self.selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("QEMU closed serial output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)
        end = self.pending.index(marker) + len(marker)
        del self.pending[:end]

    def read_until_any(self, markers: list[bytes], timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            for marker in markers:
                if marker in self.pending:
                    end = self.pending.index(marker) + len(marker)
                    del self.pending[:end]
                    return marker
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"waiting for {markers!r}; tail={self.transcript[-1000:]!r}")
            for key, _ in self.selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("QEMU closed serial output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)

    def send(self, command: str) -> None:
        self.stdin.write((command + "\n").encode())
        self.stdin.flush()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.selector.close()
        self.log_file.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", default="qemu-system-riscv64")
    parser.add_argument("--boot-disk", type=Path, required=True)
    parser.add_argument("--serial-log", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    argv = [
        args.qemu,
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "512M", "-smp", "2",
        "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={args.boot_disk},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
    ]
    boot = SerialBoot(argv, args.serial_log)
    status = "timeout"
    error = None
    started = time.time()
    try:
        boot.read_until(b"=> ", 30)
        commands = [
            ("version", "version", b"U-Boot 2026"),
            ("virtio-scan", "virtio scan", b"=> "),
            ("filesystem", "ext4ls virtio 0:0 /", b"asterinas.booti"),
            ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", b"bytes read"),
            ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", b"bytes read"),
            ("dtb-select", f"fdt addr {DTB_LOAD:#x}", b"Working FDT set"),
            ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=info init=/init asterinas.futex_profile=1 asterinas.futex_detail_min_pid=1"', b"=> "),
            ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", b"bytes read"),
            ("initrd-size", "setenv initrd_size ${filesize}", b"=> "),
            ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}", b"Starting kernel ..."),
        ]
        for _, command, marker in commands:
            boot.send(command)
            boot.read_until(marker, 30 if marker != b"Starting kernel ..." else 60)
            if marker != b"Starting kernel ..." and marker != b"=> ":
                boot.read_until(b"=> ", 30)
        marker = boot.read_until_any(
            [b"FUTEX_LOST_WAKEUP_PROBE status=pass", b"FUTEX_LOST_WAKEUP_PROBE status=fail"],
            args.timeout,
        )
        status = "pass" if marker.endswith(b"pass") else "fail"
    except Exception as exc:  # preserve the serial boundary for diagnosis
        error = repr(exc)
    finally:
        boot.close()

    result = {
        "schema": 1,
        "status": status,
        "elapsed_seconds": round(time.time() - started, 3),
        "error": error,
        "serial_log": str(args.serial_log),
        "qemu_argv": argv,
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
