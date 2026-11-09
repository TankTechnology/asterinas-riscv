#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the browser-web image in QEMU and run the workflow checks against it.

The QEMU boot is the same handoff the board uses (U-Boot loads the kernel,
device tree and stage1 initramfs from a boot disk, stage1 mounts the Debian
root and starts systemd). The guest gets a slirp NIC with `hostfwd` for port
22, so the very same `run_workflow_checks` driver runs against
127.0.0.1:<forwarded port>; the only extra work here is installing the test
key over the serial debug console, because the image ships no
authorized_keys.

    python3 -m tools.riscv.legacy.compat.run_qemu_workflow_checks --only tar_xz
"""

from __future__ import annotations

import argparse
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from tools.riscv.legacy.compat import run_workflow_checks

REPO = Path(__file__).resolve().parents[4]
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
DTB = REPO / "target/qemu-uboot/current/qemu-virt.dtb"
STAGE1 = REPO / "target/debian-riscv/stage1-qemu.cpio"
DEFAULT_KERNEL = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
DEFAULT_IMAGE = REPO / "target/debian-riscv/browser-web/rootfs/debian-root.ext2"
BOOT_DISK = REPO / "target/compat/qemu-boot.ext4"
LOG = REPO / "target/compat/qemu-workflow-serial.log"

BOOTARGS = (
    "console=ttyS0 loglevel=4 init=/init "
    "-- --root-init=systemd --debug-console=isolated-root"
)

UBOOT_COMMANDS = (
    "virtio scan",
    "ext4load virtio 0:0 0x80200000 /asterinas.booti",
    "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
    "fdt addr 0x88000000",
    "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
    "setenv initrd_size ${filesize}",
    f'setenv bootargs "{BOOTARGS}"',
    "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
)


class Serial:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = open(path, "wb", buffering=0)
        self.pending = bytearray()
        self.fd = -1

    def attach(self, fd: int) -> None:
        self.fd = fd

    def pump(self, timeout: float) -> None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            return
        if chunk:
            self.pending.extend(chunk)
            self.handle.write(chunk)

    def wait_for(self, needle: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        start = 0
        while time.monotonic() < deadline:
            self.pump(0.5)
            index = self.pending.find(needle, start)
            if index >= 0:
                return
        raise TimeoutError(f"timed out waiting for {needle!r}")

    def drain(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump(0.5)

    def send(self, text: str) -> None:
        os.write(self.fd, text.encode())


def build_boot_disk(kernel: Path) -> Path:
    BOOT_DISK.parent.mkdir(parents=True, exist_ok=True)
    if BOOT_DISK.exists():
        return BOOT_DISK
    stage = BOOT_DISK.parent / "payload"
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(kernel, stage / "asterinas.booti")
    shutil.copyfile(DTB, stage / "qemu-virt.dtb")
    shutil.copyfile(STAGE1, stage / "stage1-initramfs.cpio")
    with open(BOOT_DISK, "wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    subprocess.run(
        ["mke2fs", "-q", "-t", "ext4", "-F", "-d", str(stage), str(BOOT_DISK)],
        check=True,
    )
    subprocess.run(
        ["debugfs", "-w", "-R", "rmdir /lost+found", str(BOOT_DISK)],
        check=True,
        capture_output=True,
    )
    return BOOT_DISK


def qemu_argv(boot_disk: Path, image: Path, port: int) -> list[str]:
    return [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "4",
        "-display", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={boot_disk},id=bootdisk,readonly=on",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={image},id=rootdisk,snapshot=on",
        "-device", "virtio-blk-device,drive=rootdisk",
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-device", "virtio-net-device,netdev=net0",
        "-no-reboot",
    ]


def wait_for_port(port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL)
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="host port forwarded to guest port 22 (0 picks a free one)",
    )
    parser.add_argument("--boot-timeout", type=float, default=420.0)
    parser.add_argument("--key", default="~/.ssh/asterinas-gate")
    parser.add_argument("--only", action="append", choices=run_workflow_checks.ALL_CHECKS)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args(argv)

    for tool in ("qemu-system-riscv64", "mke2fs", "debugfs"):
        if shutil.which(tool) is None:
            print(f"error: missing {tool}", file=sys.stderr)
            return 2

    key = Path(args.key).expanduser()
    if not key.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
    public_key = Path(f"{key}.pub").read_text().strip()

    if args.port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            args.port = probe.getsockname()[1]

    boot_disk = build_boot_disk(args.kernel)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists():
        LOG.unlink()

    master, slave = pty.openpty()
    process = subprocess.Popen(
        qemu_argv(boot_disk, args.image, args.port),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=os.setsid,
    )
    os.close(slave)
    serial = Serial(LOG)
    serial.attach(master)

    try:
        serial.wait_for(b"=> ", 90)
        for command in UBOOT_COMMANDS:
            serial.send(command + "\n")
            if command.startswith("booti "):
                serial.wait_for(b"Starting kernel ...", 90)
            else:
                serial.drain(1.5)

        print("[qemu] waiting for the debug console", flush=True)
        serial.wait_for(b"root@asterinas-debug", args.boot_timeout)
        serial.drain(1)
        # The image ships no authorized_keys; install the gate key and make
        # sure sshd's runtime directory exists before the first login.
        serial.send(
            "install -d -m 0700 -o asterinas -g asterinas /home/asterinas/.ssh\n"
        )
        serial.send(
            "printf '%s\\n' '" + public_key + "' >> "
            "/home/asterinas/.ssh/authorized_keys\n"
        )
        serial.send(
            "chown asterinas:asterinas /home/asterinas/.ssh/authorized_keys; "
            "chmod 0600 /home/asterinas/.ssh/authorized_keys; "
            "mkdir -p /run/sshd; rm -f /run/nologin\n"
        )
        serial.drain(2)

        print(f"[qemu] waiting for sshd on 127.0.0.1:{args.port}", flush=True)
        if not wait_for_port(args.port, time.monotonic() + 180):
            print("error: sshd never accepted a connection", file=sys.stderr)
            return 1

        guest_args = [
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--key",
            str(key),
            # slirp hands the guest 10.0.2.15 and presents the host as 10.0.2.2.
            "--wheel-index-host",
            "10.0.2.2",
        ]
        for name in args.only or ():
            guest_args += ["--only", name]
        return run_workflow_checks.main(guest_args)
    except TimeoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_running:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        serial.handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
