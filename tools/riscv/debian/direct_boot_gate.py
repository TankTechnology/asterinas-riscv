#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot a Debian rootfs with an upstream Asterinas RISC-V kernel.

This is the first migration gate for the upstream-rebase line.  It keeps the
boot path deliberately small: QEMU direct-boots the kernel, mounts the Debian
rootfs as ``/dev/vda``, and runs an init shell over the serial console.  The
gate verifies the tools the agent runtime depends on without requiring Stage1
or the downstream QEMU/U-Boot tooling.

Run it inside the Asterinas development container, where
``qemu-system-riscv64`` is available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PROMPT = re.compile(r"root@\(none\):/# ")
GATE_END = re.compile(r"(?m)^GATE_END\r?$")
GATE_VALUE = re.compile(r"(?m)^GATE_([A-Z_]+)=(.*)$")


class GateError(RuntimeError):
    """The guest did not reach or satisfy the expected gate state."""


class Serial:
    """A small serial reader that avoids a pexpect dependency."""

    def __init__(self, process: subprocess.Popen[bytes], log_path: Path) -> None:
        self.process = process
        self.fd = process.stdout.fileno()
        self.buffer = ""
        self.log = log_path.open("w", encoding="utf-8", errors="replace")

    def close(self) -> None:
        self.log.close()

    def _read(self, timeout: float) -> str:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return ""
        chunk = os.read(self.fd, 65536)
        if not chunk:
            raise GateError("QEMU closed the serial console")
        text = chunk.decode("utf-8", "replace")
        self.buffer += text
        self.log.write(text)
        self.log.flush()
        if len(self.buffer) > 1_000_000:
            self.buffer = self.buffer[-500_000:]
        return text

    def expect(self, pattern: re.Pattern[str], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            match = pattern.search(self.buffer)
            if match is not None:
                matched = self.buffer[: match.end()]
                self.buffer = self.buffer[match.end() :]
                return matched
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError(
                    f"timeout waiting for {pattern.pattern!r}\n"
                    f"--- serial tail ---\n{self.buffer[-4000:]}"
                )
            self._read(min(remaining, 1.0))

    def send(self, line: str) -> None:
        self.process.stdin.write((line + "\n").encode())
        self.process.stdin.flush()


def build_qemu_argv(
    *,
    qemu: str,
    kernel: Path,
    rootfs: Path,
    dsh_image: Path | None,
    memory: str,
    smp: int,
    rootfstype: str | None = None,
) -> list[str]:
    append = "root=/dev/vda rw console=ttyS0 earlycon init=/bin/bash loglevel=error"
    if rootfstype is not None:
        append += f" rootfstype={rootfstype}"
    argv = [
        qemu,
        "-cpu",
        "rv64,svpbmt=true,zkr=true",
        "-machine",
        "virt",
        "-m",
        memory,
        "-smp",
        str(smp),
        "--no-reboot",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "stdio",
        "-kernel",
        str(kernel),
        "-append",
        append,
        "-drive",
        f"if=none,format=raw,id=root,file={rootfs},snapshot=on",
    ]
    if dsh_image is not None:
        argv.extend(
            [
                "-drive",
                f"if=none,format=raw,id=dsh,file={dsh_image},snapshot=on",
                "-device",
                "virtio-blk-device,drive=dsh",
            ]
        )
    # The root device must be the last virtio-blk-device so that the guest
    # discovers it as /dev/vda.  The optional DSH disk then becomes /dev/vdb.
    argv.extend(["-device", "virtio-blk-device,drive=root"])
    return argv


def guest_commands(*, dsh_image: Path | None) -> list[str]:
    commands = [
        "mount -t proc proc /proc 2>/dev/null || true",
        "mount -t sysfs sysfs /sys 2>/dev/null || true",
        "mount -t devtmpfs devtmpfs /dev 2>/dev/null || true",
        # `init=/bin/bash` starts with an empty environment.  Bash then sets a
        # default PATH for itself but does not export it, so gcc inherits no
        # PATH and cannot locate cc1 or ld from its own relative search path.
        # A normal login/systemd session exports PATH; mirror that here.
        "export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
        "echo GATE_BEGIN",
        "printf 'GATE_KERNEL=%s\\n' \"$(uname -srm)\"",
        "printf 'GATE_GCC=%s\\n' \"$(gcc -dumpversion 2>/dev/null)\"",
        "echo 'int main(void){return 42;}' > /tmp/gate_cc.c",
        "gcc /tmp/gate_cc.c -o /tmp/gate_cc && /tmp/gate_cc; "
        "printf 'GATE_CC_RC=%s\\n' \"$?\"",
        "printf 'GATE_PYTHON=%s\\n' \"$(python3 -V 2>&1)\"",
        "printf 'GATE_NODE=%s\\n' \"$(node -v 2>/dev/null)\"",
        "printf 'GATE_GIT=%s\\n' \"$(git --version 2>/dev/null)\"",
    ]
    if dsh_image is not None:
        commands.extend(
            [
                "mkdir -p /mnt/dsh",
                "mount -t ext2 /dev/vdb /mnt/dsh || echo GATE_DSH_MOUNT=failed",
                "printf 'GATE_DSH_BIN=%s\\n' "
                "\"$(test -f /mnt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js "
                "&& echo yes || echo no)\"",
                "printf 'GATE_DSH_HELP=%s\\n' "
                "\"$(node --expose-internals "
                "/mnt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js --help "
                "2>&1 | head -1)\"",
            ]
        )
    commands.append("echo GATE_END")
    return commands


def check_values(values: dict[str, str], *, dsh_image: Path | None) -> None:
    expected = {
        "KERNEL": ("riscv64",),
        "GCC": ("14",),
        "CC_RC": ("42",),
        "PYTHON": ("Python 3.13",),
        "NODE": ("v24.",),
        "GIT": ("git version",),
    }
    for key, needles in expected.items():
        value = values.get(key, "")
        if not all(needle in value for needle in needles):
            raise GateError(f"GATE_{key} mismatch: {value!r}")
    if dsh_image is not None:
        if values.get("DSH_BIN") != "yes":
            raise GateError("dsh binary is missing from the attached DSH image")
        if not values.get("DSH_HELP", "").startswith("Usage: dsh"):
            raise GateError(f"dsh help mismatch: {values.get('DSH_HELP')!r}")


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    qemu = shutil.which(args.qemu)
    if qemu is None:
        raise GateError(f"qemu executable not found: {args.qemu}")
    if not args.kernel.is_file():
        raise GateError(f"kernel not found: {args.kernel}")
    if not args.rootfs.is_file():
        raise GateError(f"rootfs not found: {args.rootfs}")
    if args.dsh_image is not None and not args.dsh_image.is_file():
        raise GateError(f"DSH image not found: {args.dsh_image}")

    args.log.parent.mkdir(parents=True, exist_ok=True)
    argv = build_qemu_argv(
        qemu=qemu,
        kernel=args.kernel,
        rootfs=args.rootfs,
        dsh_image=args.dsh_image,
        memory=args.memory,
        smp=args.smp,
        rootfstype=args.rootfstype,
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    serial = Serial(process, args.log)
    try:
        serial.expect(PROMPT, args.timeout)
        for command in guest_commands(dsh_image=args.dsh_image):
            serial.send(command)
        output = serial.expect(GATE_END, args.timeout)
        values = {
            key: value.strip()
            for key, value in GATE_VALUE.findall(ANSI.sub("", output))
        }
        check_values(values, dsh_image=args.dsh_image)
        serial.send("poweroff -f")
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
        return {
            "status": "pass",
            "kernel": str(args.kernel),
            "rootfs": str(args.rootfs),
            "dsh_image": str(args.dsh_image) if args.dsh_image else None,
            "values": values,
            "log": str(args.log),
        }
    except GateError as error:
        os.killpg(process.pid, signal.SIGTERM)
        raise GateError(
            f"{error}\n--- serial tail ---\n{serial.buffer[-4000:]}"
        ) from error
    finally:
        serial.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--dsh-image", type=Path)
    parser.add_argument("--rootfstype", choices=("ext2", "ext4"))
    parser.add_argument("--qemu", default="qemu-system-riscv64")
    parser.add_argument("--memory", default="2G")
    parser.add_argument("--smp", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("target/riscv-debian-direct.log"),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        result = run_gate(args)
    except GateError as error:
        print(json.dumps({"status": "fail", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
