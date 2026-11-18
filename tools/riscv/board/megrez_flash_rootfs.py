#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Write a freshly built rootfs image over the board's running root partition.

The board has no second root to boot from, so the image is written through the
kernel's own block device while the system runs from it.  That is only safe if
two things hold, and this script enforces both:

* the image is verified before it is written (`sha256sum` on the board), and
* the partition is read back and verified afterwards, before the reboot.

The root filesystem stays mounted read-write (remounting it read-only needs
every writer stopped, and stopping the desktop session hangs on this kernel),
so the risk is a dirty page flushed after the write.  The read-back comparison
is what catches it: if the partition does not hash to the image, nothing is
rebooted and the write can simply be repeated.

    megrez_flash_rootfs.py --image debian-root.ext2 --url http://host:18090/

Requires the console bridge (`megrez_console.py bridge`) and an HTTP server
serving the image directory.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from megrez_console import Console  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PARTITION = "/dev/mmcblk0p2"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Flash:
    def __init__(self, log: str, fifo: str) -> None:
        self.log = Path(log)
        self.console = Console(str(self.log), fifo)
        self.counter = 0

    def since(self, offset: int) -> str:
        with self.log.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        return ANSI.sub("", data.decode("utf-8", "replace"))

    def _send(self, data: bytes, attempts: int = 6) -> None:
        """Write to the bridge, retrying the FIFO open/write race.

        The bridge opens the FIFO, reads until the writers close, and reopens.
        A writer that opens in the gap after the reader closed its end gets
        EPIPE, which happens most at fast character pacing.
        """
        for attempt in range(attempts):
            try:
                self.console.send_raw(data)
                return
            except (BrokenPipeError, OSError):
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5)

    def reset_line(self) -> None:
        """Discard whatever a previous interrupted command left on the line."""

        self._send(b"\x03")
        time.sleep(0.5)
        self._send(b"\n")
        time.sleep(2)

    def command(self, text: str, timeout: float, pause: float = 0.05) -> str:
        self.counter += 1
        marker = f"ZZFLASH_{self.counter}"
        self.reset_line()
        offset = self.log.stat().st_size
        # `type_line` frames the line with a carriage return and the raw-mode
        # toggles; typing the characters by hand here left the line unterminated
        # and every command silently never ran.
        for attempt in range(4):
            try:
                self.console.type_line(f"{text}; echo {marker}", pause)
                break
            except (BrokenPipeError, OSError):
                if attempt == 3:
                    raise
                self.reset_line()
        pattern = re.compile(rf"^{re.escape(marker)}\s*$", re.MULTILINE)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pattern.search(self.since(offset)):
                break
            time.sleep(2)
        time.sleep(1)
        return self.since(offset)

    def run(self, image: Path, url: str, compressed: bool = False) -> int:
        expected = sha256_of(image)
        print(f"image {image.name} sha256={expected}", flush=True)

        text = self.command("date -u +%s", timeout=60)
        if not re.search(r"\b\d{10}\b", text):
            print("no shell on the console", file=sys.stderr)
            return 2

        # The image goes to tmpfs: it is 2 GiB and the root partition has to be
        # the only thing worth trusting at this point.
        target = "/tmp/rootfs-flash.img"
        print("downloading the image to the board's tmpfs", flush=True)
        # The board-to-host path runs at about 2 MiB/s here, so the image is
        # usually shipped gzipped: 3 GiB of ext2 is mostly free blocks and
        # compresses to a fraction of that.
        if compressed:
            download = (
                f"rm -f {target}; curl -m 3600 -s {url}{image.name}.gz | "
                f"gunzip > {target}; wc -c < {target}"
            )
        else:
            download = (
                f"rm -f {target}; curl -m 3600 -s -o {target} {url}{image.name}; "
                f"wc -c < {target}"
            )
        text = self.command(download, timeout=3700)
        size_match = re.search(r"^(\d+)$", text, re.MULTILINE)
        if not size_match or int(size_match.group(1)) != image.stat().st_size:
            print(f"download incomplete: {text[-300:]}", file=sys.stderr)
            return 3

        print("verifying the download", flush=True)
        text = self.command(f"sha256sum {target}", timeout=300)
        if expected not in text:
            print(f"image hash mismatch on the board: {text[-200:]}", file=sys.stderr)
            return 4

        # Freeze the filesystem first. Without this the running system keeps
        # flushing dirty pages over the freshly written image between the write
        # and the read-back, and the read-back differs (measured: two pages).
        print("remounting the root read-only", flush=True)
        text = self.command("mount -o remount,ro / ; echo RM_RC=$?", timeout=120)
        if "RM_RC=0" not in text:
            print(f"could not remount the root read-only: {text[-200:]}", file=sys.stderr)
            return 7

        # Write and reboot from one command line. After the write the running
        # system's cached blocks no longer match what is on the device, so a
        # binary it has to re-read fails with "Exec format error" and even
        # `reboot` stops working; the shell that is already running can still
        # execute it. The image is verified after the boot instead, from the
        # system that came up from it.
        print(f"writing {image.stat().st_size} bytes over {PARTITION}", flush=True)
        # The reboot must not execute a binary: after the write the running
        # system's cached blocks no longer match the device, and exec of an
        # uncached binary fails ("Permission denied"/"Exec format error").
        # `echo` is a shell builtin and the sysrq path lives in the kernel.
        text = self.command(
            f"dd if={target} of={PARTITION} bs=1M conv=fsync 2>&1 | tail -1; "
            # `command()` appends and matches its own marker line; a literal
            # one here would only invite matching the echo of the command.
            "echo b > /proc/sysrq-trigger",
            timeout=1800,
        )
        if "copied" not in text:
            print(f"dd did not report success: {text[-300:]}", file=sys.stderr)
            return 5
        print("rebooting into the new image", flush=True)

        if not self.wait_for_boot():
            print("the board did not come back after the reboot", file=sys.stderr)
            return 8

        print("verifying the partition from the new system", flush=True)
        blocks = image.stat().st_size // (1 << 20)
        text = self.command(
            f"dd if={PARTITION} bs=1M count={blocks} 2>/dev/null | sha256sum",
            timeout=1800,
        )
        if expected not in text:
            print(
                "the image on the partition does not match the built image",
                file=sys.stderr,
            )
            return 6
        print(f"verified: {PARTITION} matches the built image", flush=True)
        return 0

    def wait_for_boot(self, timeout: float = 300.0) -> bool:
        # The shell prompt is present before the reboot too, so wait for
        # evidence that a new kernel actually started before accepting it.
        offset = self.log.stat().st_size
        deadline = time.time() + timeout
        rebooted = False
        while time.time() < deadline:
            text = self.since(offset)
            if "Starting kernel" in text or "OpenSBI" in text:
                rebooted = True
            if rebooted and (
                "asterinas-desktop-ready" in text or "asterinas-debug:/" in text[-4000:]
            ):
                return True
            time.sleep(5)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(prog="megrez_flash_rootfs")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--url", required=True, help="HTTP prefix serving the image")
    parser.add_argument("--log", default="/tmp/board2.log")
    parser.add_argument("--fifo", default="/tmp/board2.cmd")
    parser.add_argument("--compressed", action="store_true",
                        help="the server also has <image>.gz; stream it through gunzip")
    values = parser.parse_args()
    if not values.url.endswith("/"):
        values.url += "/"
    return Flash(values.log, values.fifo).run(values.image, values.url, values.compressed)


if __name__ == "__main__":
    raise SystemExit(main())
