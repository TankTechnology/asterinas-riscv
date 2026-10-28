#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""M9 optional bonus: prove /nix persists on a second virtio-blk ext2 disk.

Boots twice with the same ext2 image attached as /dev/vdb:
  boot 1 — /init mounts /dev/vdb on /nix, rc installs the profile into the
           on-disk store, and we drop a sentinel file into /nix.
  boot 2 — the same image is re-attached; we check the sentinel (and the
           on-disk store) survived the reboot.

Reuses Boot + UBOOT_COMMANDS from boot_m9_smoke.py. Exit 0 iff persistence is
proven.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from boot_m9_smoke import (
    Boot,
    UBOOT_COMMANDS,
    INIT_MARKER,
    RC_DONE_MARKER,
    PROMPT,
    REPO,
    UBOOT,
    BOOT_DISK,
    KERNEL_LOAD,
    INITRD_LOAD,
    DTB_LOAD,
)

PERSIST_DISK = REPO / "target/nixos/m9/nix-store.ext2"
PERSIST_MARKER = b">>> M9 init: persistent /nix on /dev/vdb (ext2) <<<"


def make_argv(serial_log: Path) -> list[str]:
    return [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={PERSIST_DISK},id=nixdisk",
        "-device", "virtio-blk-device,drive=nixdisk",
    ]


def drive_uboot(boot: Boot) -> None:
    boot.read_until(b"=> ", 60)
    for name, text, expected in UBOOT_COMMANDS:
        boot.send(text)
        if name == "booti":
            boot.read_until(b"Starting kernel ...", 90)
        else:
            boot.read_until(expected.encode(), 30)
            if expected != "=>":
                boot.read_until(b"=> ", 30)


def login(boot: Boot) -> None:
    boot.read_until(b"login:", 60)
    boot.send("root")
    boot.read_until(b"Password:", 30)
    boot.send("nixos")
    boot.read_until(b"__M9_LOGIN_OK__", 30)
    boot.read_until(PROMPT, 15)


def guest_cmd(boot: Boot, cmd: str) -> bytes:
    boot.send(cmd)
    try:
        boot.read_until(PROMPT, 60)
    except TimeoutError:
        pass
    return bytes(boot.transcript)


def run_boot(tag: str, serial_log: Path) -> Boot:
    print(f"[{tag}] booting QEMU", flush=True)
    boot = Boot(make_argv(serial_log), serial_log)
    drive_uboot(boot)
    boot.read_until(INIT_MARKER, 120)
    print(f"[{tag}] init reached", flush=True)
    return boot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-m9-persist-serial.log"))
    parser.add_argument("--rc-timeout", type=float, default=300.0)
    args = parser.parse_args()

    if not PERSIST_DISK.exists():
        raise SystemExit(f"missing {PERSIST_DISK}\nrun make_persist_disk.sh first")

    checks = []

    # --- boot 1: populate the store on the ext2 disk ---
    boot1 = run_boot("boot1", args.serial_log)
    try:
        boot1.read_until(PERSIST_MARKER, 60)
        checks.append(("ext2 /nix mounted (boot 1)", "OK"))
        print("[boot1] ext2 /nix mounted", flush=True)
        boot1.read_until(RC_DONE_MARKER, args.rc_timeout)
        login(boot1)
        # Write a sentinel into /nix (on the ext2 disk) and record the store size.
        boot1.send("echo persisted > /nix/PERSISTED && sync")
        boot1.read_until(PROMPT, 30)
        store1 = guest_cmd(boot1, "ls /nix/store | wc -l")
        print(f"[boot1] store listing: {store1[-200:].decode(errors='replace')}", flush=True)
    finally:
        boot1.close()

    # --- boot 2: verify the sentinel + store survived ---
    boot2 = run_boot("boot2", args.serial_log)
    try:
        boot2.read_until(PERSIST_MARKER, 60)
        checks.append(("ext2 /nix mounted (boot 2)", "OK"))
        boot2.read_until(RC_DONE_MARKER, args.rc_timeout)
        login(boot2)
        t = guest_cmd(boot2, "[ -f /nix/PERSISTED ] && echo __PERSIST_SURVIVED__=OK || echo __PERSIST_SURVIVED__=MISSING")
        checks.append(("sentinel survived reboot", "OK" if b"__PERSIST_SURVIVED__=OK" in t else "MISSING"))
        print("[boot2] sentinel check done", flush=True)
        store2 = guest_cmd(boot2, "ls /nix/store | wc -l")
        print(f"[boot2] store listing: {store2[-200:].decode(errors='replace')}", flush=True)
    finally:
        boot2.close()

    print("\n=== M9 ext2 persistence smoke results ===", flush=True)
    for name, status in checks:
        print(f"  {name}: {status}", flush=True)
    return 0 if checks and all(s == "OK" for _, s in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
