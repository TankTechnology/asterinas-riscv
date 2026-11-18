#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Prepare, run and read back one diagnostic boot on the Megrez board.

The board stalls about a minute after the desktop starts and only the
hardware watchdog brings it back. Nothing on the running system can be asked
afterwards -- the reset takes RAM with it -- so the evidence has to be
prepared *before* the boot and read out of the root filesystem after it:

1. park at U-Boot and boot the vendor initramfs (a normal Linux userland);
2. find the SD card (its `mmcblk` index varies between boots) and mount p2;
3. install the durable uptime heartbeat, restore the desktop unit an earlier
   experiment masked, and make sure `/var/log/journal` exists *now* -- journald
   only switches to the persistent directory when it exists when journald
   starts, and a directory created from a later root shell leaves the running
   instance writing to /run, which the reset throws away;
4. boot the kernel with the *saved* `bootcmd`: it arms the watchdog and
   carries `asterinas.net`, and nothing is typed while it runs;
5. after the watchdog reset, boot the vendor initramfs again and read
   `/var/log/asterinas-heartbeat` plus the dead boot's journal with
   `journalctl -D /var/log/journal -b -1 -k`.

Usage:

    megrez_diag_round.py prepare   # steps 1-4; ends with the board booting
    megrez_diag_round.py journal   # step 5, once the board has reset

The console bridge must already be running (`megrez_console.py bridge`); its
transcript and command FIFO default to the paths that bridge uses.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

LOG = os.environ.get("ASTERINAS_BOARD_LOG", "/tmp/megrez2.log")
FIFO = os.environ.get("ASTERINAS_BOARD_FIFO", "/tmp/megrez.cmd")
REPOSITORY_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")


def size() -> int:
    return os.path.getsize(LOG)


def since(offset: int) -> str:
    with open(LOG, "rb") as handle:
        handle.seek(offset)
        return ANSI.sub("", handle.read().decode("utf-8", "replace")).replace("\r", "")


def send(line: str) -> None:
    for _ in range(6):
        try:
            with open(FIFO, "w") as handle:
                handle.write(line + "\n")
            return
        except OSError:
            time.sleep(0.4)


def run(line: str, expect: str, timeout: float = 120.0, tries: int = 4):
    for _ in range(tries):
        offset = size()
        send(line)
        end = time.time() + timeout
        while time.time() < end:
            text = since(offset)
            if re.search(expect, text):
                return text
            time.sleep(0.5)
    return None


def park(timeout: float = 1800.0) -> bool:
    print("waiting for U-Boot ...", flush=True)
    offset = size()
    countdown_until = 0.0
    end = time.time() + timeout
    while time.time() < end:
        recent = since(offset)
        offset = size()
        if re.search(r"(?m)^=>\s*$", recent):
            return True
        if "Hit any key to stop autoboot" in recent:
            countdown_until = time.time() + 40
        if time.time() < countdown_until:
            send(" ")
        time.sleep(0.25)
    return False


VENDOR = (
    ("ext4load mmc 1:1 0x84000000 /vmlinuz-6.6.87-win2030-origin", r"9706299 bytes read"),
    ("ext4load mmc 1:1 0x88300000 /initrd.img-6.6.87-win2030", r"62583027 bytes read"),
    ("ext4load mmc 1:1 0x88000000 /dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
     r"154800 bytes read"),
    ("setenv bootargs console=ttyS0,115200 earlycon", r"earlycon"),
    ("booti 0x84000000 0x88300000:0x3BAF0F3 0x88000000", r"\(initramfs\)"),
)


def vendor_shell():
    """Boot the vendor initramfs and wait for its shell prompt."""

    for line, expect in VENDOR:
        if run(line, expect, timeout=120) is None:
            print("FAILED:", line, flush=True)
            return False
    time.sleep(3)
    for line, expect in (
        ("mount -t proc proc /proc", r"\(initramfs\)"),
        ("mount -t sysfs sys /sys", r"\(initramfs\)"),
        ("mkdir -p /mnt2", r"\(initramfs\)"),
    ):
        run(line, expect)
    return True


def mount_root() -> bool:
    """Mount p2; the mmcblk index is not stable across boots."""

    text = run("ls /dev/mmcblk*p2", r"mmcblk\d+p2")
    for candidate in sorted(set(re.findall(r"(mmcblk\d+p2)", text or ""))):
        if run(f"mount -t ext4 /dev/{candidate} /mnt2; echo M2=$?", r"M2=0") is not None:
            print("mounted", candidate, flush=True)
            return True
    print("could not mount any p2", flush=True)
    return False


def install_helpers(script_path: str, unit_path: str) -> bool:
    steps = (
        ("chroot /mnt2 /usr/bin/mkdir -p /usr/lib/asterinas /var/log/journal; echo J1=$?",
         r"J1=0"),
        # The initramfs has no cp: cat is the transfer that exists here.
        (f"cat {script_path} > /mnt2/usr/lib/asterinas/asterinas-heartbeat; echo H1=$?", r"H1=0"),
        (f"cat {unit_path} > /mnt2/etc/systemd/system/asterinas-heartbeat.service; echo H2=$?", r"H2=0"),
        ("chmod 755 /mnt2/usr/lib/asterinas/asterinas-heartbeat; echo H3=$?", r"H3=0"),
        ("ln -sf /etc/systemd/system/asterinas-heartbeat.service "
         "/mnt2/etc/systemd/system/sysinit.target.wants/asterinas-heartbeat.service; echo H4=$?", r"H4=0"),
        # Undo the mask an earlier experiment left behind.
        ("if [ -f /mnt2/etc/systemd/system/asterinas-desktop-m5.service.orig ]; then "
         "mv -f /mnt2/etc/systemd/system/asterinas-desktop-m5.service.orig "
         "/mnt2/etc/systemd/system/asterinas-desktop-m5.service; fi; echo R1=$?", r"R1=0"),
        # The console echoes the typed line, so a bare `echo SYNCD` is matched
        # by its own echo before sync has finished. Build it from a variable.
        ("M=SYN; sync; echo ${M}CD", r"SYNCD", 180),
    )
    for item in steps:
        line, expect = item[0], item[1]
        timeout = item[2] if len(item) > 2 else 120
        if run(line, expect, timeout=timeout) is None:
            print("FAILED:", line, flush=True)
            return False
    return True


def prepare() -> int:
    if not park():
        return 1
    if not vendor_shell():
        return 2
    if not mount_root():
        return 3
    source_directory = os.path.join(REPOSITORY_ROOT, "tools/riscv/debian/rootfs")
    if not install_helpers(
        os.path.join(source_directory, "asterinas-heartbeat.sh"),
        os.path.join(source_directory, "asterinas-heartbeat.service"),
    ):
        return 4
    print("heartbeat installed, desktop unmasked, journal directory prepared", flush=True)
    if run("/usr/bin/reboot -f", r"U-Boot 2024|Hit any key", timeout=90) is None:
        return 5
    if not park():
        return 6
    run("run bootcmd", r"Starting kernel", timeout=90)
    print("booted with the saved bootcmd; not touching the console", flush=True)
    return 0


def journal() -> int:
    if not park():
        return 1
    if not vendor_shell():
        return 2
    if not mount_root():
        return 3
    print(run("chroot /mnt2 /usr/bin/cat /var/log/asterinas-heartbeat", r"uptime=")
          or "no heartbeat", flush=True)
    print(run("chroot /mnt2 /usr/bin/journalctl -D /var/log/journal --list-boots --no-pager | tail -3",
              r"\d") or "no boots listed", flush=True)
    print(run("chroot /mnt2 /usr/bin/journalctl -D /var/log/journal -b -1 -k --no-pager | tail -40",
              r"kernel:|-- No entries", timeout=120) or "", flush=True)
    return 0


def main(argv: list[str]) -> int:
    global LOG, FIFO
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=("prepare", "journal"))
    parser.add_argument("--log", default=LOG, help="console transcript (default %(default)s)")
    parser.add_argument("--fifo", default=FIFO, help="console command fifo (default %(default)s)")
    args = parser.parse_args(argv)

    LOG, FIFO = args.log, args.fifo
    return prepare() if args.stage == "prepare" else journal()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
