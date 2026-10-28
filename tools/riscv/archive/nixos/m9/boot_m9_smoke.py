#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the M9 lightweight-NixOS demo on Asterinas RISC-V and exercise it.

This is an *interactive* smoke test, unlike M1-M8: after the kernel boots,
busybox init runs /etc/rc (installs the nix profile, starts services) and then
spawns a getty/login loop on ttyS0. The test:
  1. drives U-Boot to hand off to the kernel (same as M8)
  2. waits for rc to finish and for getty's "login:" prompt
  3. logs in as root (password "nixos")
  4. runs several nix-installed binaries from the login shell
  5. checks the nix-managed heartbeat service is alive
  6. logs out and confirms getty respawns (the login loop)

Exit code 0 iff every check passes.
"""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

INIT_MARKER = b">>> M9 init: lightweight NixOS"
RC_DONE_MARKER = b"__M9_RC_DONE__"
PROMPT = b"NIXOS# "

UBOOT_COMMANDS = [
    ("version", "version", "U-Boot 2026"),
    ("virtio-scan", "virtio scan", "=>"),
    ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
    ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
    ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
    ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
    ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
    ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
    ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
    ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
     "Starting kernel ..."),
]

# The keywords that any fortune output must match (one of the six quips).
FORTUNE_KEYWORDS = [
    b"OS kernel", b"package manager", b"instruction set",
    b"systemd", b"content-addressed", b"rule them all",
]

# (name, guest command, expected substrings)
GUEST_CHECKS = [
    ("hello", "hello", [b"Hello, world!"]),
    ("nixos-info", "nixos-info", [b"hostname", b"kernel"]),
    ("fortune", "fortune", None),  # handled specially (any-of keywords)
    ("curl --version", "curl --version", [b"curl 8.21.0"]),
    ("jq --version", "jq --version", [b"jq-1.8.2"]),
    ("heartbeat service", (
        "echo __M9_HEARTBEAT__=$([ -s /var/log/heartbeat.log ] "
        "&& echo OK || echo EMPTY)"
    ), [b"__M9_HEARTBEAT__=OK"]),
    ("services running", (
        "for p in syslogd crond heartbeat; do "
        "echo __M9_SVC_${p}__=$(pidof $p >/dev/null 2>&1 "
        "&& echo OK || echo MISSING); done"
    ), [b"__M9_SVC_syslogd__=OK", b"__M9_SVC_crond__=OK", b"__M9_SVC_heartbeat__=OK"]),
]


class Boot:
    def __init__(self, argv: list[str], serial_log: Path) -> None:
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.pending = bytearray()
        self.transcript = bytearray()

    def read_until(self, needle: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for {needle!r}; tail={self.transcript[-600:]!r}"
                )
            for key, _ in self.sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return consumed

    def send(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        self.sel.close()
        self.log_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-m9-serial.log"))
    parser.add_argument("--rc-timeout", type=float, default=300.0)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}\nrun prepare_qemu_uboot_booti.sh first")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}\nrun prepare_qemu_uboot_booti.sh first")

    argv = [
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
    ]

    boot = Boot(argv, args.serial_log)
    results = []
    final_tail = b""

    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)

        for name, text, expected in UBOOT_COMMANDS:
            print(f"[uboot] {name}", flush=True)
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print("[boot] waiting for /init", flush=True)
        boot.read_until(INIT_MARKER, 120)
        print("[ok] init reached", flush=True)

        print("[boot] waiting for rc (nix profile install + services)", flush=True)
        boot.read_until(RC_DONE_MARKER, args.rc_timeout)
        print("[ok] rc done", flush=True)

        print("[boot] waiting for getty login prompt", flush=True)
        boot.read_until(b"login:", 60)
        print("[ok] getty up", flush=True)

        # --- login ---
        print("[login] sending username 'root'", flush=True)
        boot.send("root")
        boot.read_until(b"Password:", 30)
        print("[login] sending password", flush=True)
        boot.send("nixos")
        boot.read_until(b"__M9_LOGIN_OK__", 30)
        boot.read_until(PROMPT, 15)
        print("[ok] logged in, profile activated", flush=True)

        # --- run the nix-installed binaries ---
        for name, cmd, expects in GUEST_CHECKS:
            print(f"[guest] {name}", flush=True)
            boot.send(cmd)
            try:
                boot.read_until(PROMPT, 60)
            except TimeoutError:
                pass  # the check below will mark MISSING
            transcript = bytes(boot.transcript)
            if name == "fortune":
                ok = any(k in transcript for k in FORTUNE_KEYWORDS)
            else:
                ok = all(e in transcript for e in expects)
            results.append((name, "OK" if ok else "MISSING"))
            print(f"[guest] {name}: {'OK' if ok else 'MISSING'}", flush=True)

        # --- logout and confirm the getty/login loop respawns ---
        print("[guest] logout (expect getty respawn)", flush=True)
        boot.send("exit")
        boot.read_until(b"login:", 60)
        results.append(("getty respawn after logout", "OK"))
        print("[guest] getty respawn: OK", flush=True)

        time.sleep(1)
        final_tail = bytes(boot.transcript[-4000:])
    except TimeoutError as e:
        final_tail = bytes(boot.transcript[-3000:])
        print(f"[smoke] TIMEOUT: {e}", flush=True)
        results.append(("boot/login sequence", "TIMEOUT"))
    finally:
        boot.close()

    print("\n=== M9 lightweight-NixOS smoke results ===", flush=True)
    for name, status in results:
        print(f"  {name}: {status}", flush=True)
    if final_tail:
        print("\n--- serial tail ---", flush=True)
        print(final_tail.decode(errors="replace")[-2500:], flush=True)

    return 0 if results and all(s == "OK" for _, s in results) else 1


if __name__ == "__main__":
    sys.exit(main())
