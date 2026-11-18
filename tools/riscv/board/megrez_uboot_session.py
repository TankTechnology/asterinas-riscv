#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Drive the Megrez board's serial console without losing commands.

Every operation here exists because the ad-hoc version of it cost hours:

* The console bridge drops a FIFO line now and then, and it paces bytes at
  ``TYPING_PACE_MS`` (20 ms, i.e. 50 B/s by default). Feeding it faster does not
  lose the extra lines, it *queues* them, so a later command lands minutes
  later and can truncate a file that is still being built. ``command()``
  therefore waits for the command's own result before returning.
* Long command lines are the ones that fail. Anything above ~100 characters is
  split into ``setenv``-style pieces instead of typed in one go.
* The vendor initramfs has no ``wc``, ``cp``, or ``head``. Use
  ``chroot /mnt2 /usr/bin/...`` for tools and ``cat`` for copies.

Usage:

    megrez_uboot_session.py park                 # stop autoboot, wait for =>
    megrez_uboot_session.py run '<uboot command>' --expect '<regex>'
    megrez_uboot_session.py boot-vendor-initramfs  # (initramfs) shell on ttyS0
    megrez_uboot_session.py install-kernel /root/kernel-rel.img
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

LOG = "/tmp/megrez2.log"
FIFO = "/tmp/megrez.cmd"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

VENDOR_KERNEL = "/vmlinuz-6.6.87-win2030-origin"
VENDOR_INITRD = "/initrd.img-6.6.87-win2030"
VENDOR_DTB = "/dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb"
VENDOR_INITRD_SIZE = "0x3BAF0F3"


def log_size() -> int:
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


def command(line: str, expect: str, timeout: float = 90.0, tries: int = 4) -> str | None:
    """Send one line and wait until `expect` appears in the output it caused."""
    for _ in range(tries):
        offset = log_size()
        send(line)
        end = time.time() + timeout
        while time.time() < end:
            text = since(offset)
            if re.search(expect, text):
                return text
            time.sleep(0.5)
    return None


def park(timeout: float = 1500.0) -> bool:
    """Hold the autoboot countdown open until U-Boot answers with `=>`."""
    offset = log_size()
    countdown_until = 0.0
    end = time.time() + timeout
    while time.time() < end:
        recent = since(offset)
        offset = log_size()
        if re.search(r"(?m)^=>\s*$", recent):
            return True
        if "Hit any key to stop autoboot" in recent:
            countdown_until = time.time() + 40
        if time.time() < countdown_until:
            send(" ")
        time.sleep(0.25)
    return False


def boot_vendor_initramfs() -> bool:
    steps = (
        (f"ext4load mmc 1:1 0x84000000 {VENDOR_KERNEL}", r"9706299 bytes read"),
        (f"ext4load mmc 1:1 0x88300000 {VENDOR_INITRD}", r"62583027 bytes read"),
        (f"ext4load mmc 1:1 0x88000000 {VENDOR_DTB}", r"154800 bytes read"),
        ("setenv bootargs console=ttyS0,115200 earlycon", r"earlycon"),
        (f"booti 0x84000000 0x88300000:{VENDOR_INITRD_SIZE} 0x88000000", r"\(initramfs\)"),
    )
    for line, expect in steps:
        if command(line, expect, timeout=120) is None:
            print(f"failed: {line}", file=sys.stderr)
            return False
    return True


def install_kernel(remote: str) -> bool:
    """Write an already-staged kernel file into p3 (what bootcmd loads).

    `remote` is the path **inside the board's Debian root**, e.g.
    /root/kernel-rel.img. The copy has to run outside the chroot, because the
    chroot's /mnt3 is a directory on p2, not the p3 partition -- getting that
    wrong silently keeps the old kernel in place.
    """
    steps = (
        ("mkdir -p /mnt2 /mnt3", r"\(initramfs\)"),
        ("mount -t ext4 /dev/mmcblk1p2 /mnt2; echo M2=$?", r"M2=0"),
        ("mount -t ext4 /dev/mmcblk1p3 /mnt3; echo M3=$?", r"M3=0"),
        (f"cat /mnt2{remote} > /mnt3/kernel-rel.img; echo CP=$?", r"CP=0", 180),
        # The console echoes the typed line, so `echo SYNCD` would be matched by
        # its own echo before sync returned -- the exact wait this step exists
        # for is the one that hangs on a wedged filesystem. Build the marker
        # from a variable: the echo keeps the unexpanded form.
        ("M=SYN; sync; echo ${M}CD", r"SYNCD", 180),
        ("mount -t ext4 /dev/mmcblk1p3 /mnt2/mnt3; echo M3B=$?", r"M3B=0"),
        ("chroot /mnt2 /usr/bin/sha256sum /mnt3/kernel-rel.img", r"[0-9a-f]{64}"),
    )
    for item in steps:
        line, expect = item[0], item[1]
        timeout = item[2] if len(item) > 2 else 90
        text = command(line, expect, timeout=timeout)
        if text is None:
            print(f"failed: {line}", file=sys.stderr)
            return False
    return True


def main() -> int:
    global LOG, FIFO
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=LOG)
    parser.add_argument("--fifo", default=FIFO)
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("park", help="stop autoboot and wait for the U-Boot prompt")

    run = sub.add_parser("run", help="send one command and wait for its result")
    run.add_argument("line")
    run.add_argument("--expect", required=True)
    run.add_argument("--timeout", type=float, default=90.0)

    sub.add_parser("boot-vendor-initramfs", help="boot the vendor kernel into (initramfs)")

    install = sub.add_parser("install-kernel", help="copy a staged kernel into p3")
    install.add_argument("remote", help="path inside the board's Debian root")

    args = parser.parse_args()
    LOG, FIFO = args.log, args.fifo

    if args.action == "park":
        return 0 if park() else 1
    if args.action == "run":
        text = command(args.line, args.expect, timeout=args.timeout)
        if text is None:
            print("no matching output", file=sys.stderr)
            return 1
        print("\n".join(text.splitlines()[-6:]))
        return 0
    if args.action == "boot-vendor-initramfs":
        return 0 if boot_vendor_initramfs() else 1
    if args.action == "install-kernel":
        return 0 if install_kernel(args.remote) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
