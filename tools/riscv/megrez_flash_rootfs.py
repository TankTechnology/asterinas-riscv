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

    def command(self, text: str, timeout: float, pause: float = 0.05) -> str:
        self.counter += 1
        marker = f"ZZFLASH_{self.counter}"
        offset = self.log.stat().st_size
        self.console.type_line(f"{text}; echo {marker}", pause)
        pattern = re.compile(rf"^{re.escape(marker)}\s*$", re.MULTILINE)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pattern.search(self.since(offset)):
                break
            time.sleep(2)
        time.sleep(1)
        return self.since(offset)

    def run(self, image: Path, url: str) -> int:
        expected = sha256_of(image)
        print(f"image {image.name} sha256={expected}", flush=True)

        text = self.command("date -u +%s", timeout=40)
        if not re.search(r"^\d{10}$", text, re.MULTILINE):
            print("no shell on the console", file=sys.stderr)
            return 2

        # The image goes to tmpfs: it is 2 GiB and the root partition has to be
        # the only thing worth trusting at this point.
        target = "/tmp/rootfs-flash.img"
        print("downloading the image to the board's tmpfs", flush=True)
        text = self.command(
            f"rm -f {target}; curl -m 900 -s -o {target} {url}debian-root.ext2; "
            f"wc -c < {target}",
            timeout=1000,
            pause=0.02,
        )
        size_match = re.search(r"^(\d+)$", text, re.MULTILINE)
        if not size_match or int(size_match.group(1)) != image.stat().st_size:
            print(f"download incomplete: {text[-300:]}", file=sys.stderr)
            return 3

        print("verifying the download", flush=True)
        text = self.command(f"sha256sum {target}", timeout=300)
        if expected not in text:
            print(f"image hash mismatch on the board: {text[-200:]}", file=sys.stderr)
            return 4

        print(f"writing {image.stat().st_size} bytes over {PARTITION}", flush=True)
        text = self.command(
            f"dd if={target} of={PARTITION} bs=1M conv=fsync 2>&1 | tail -2",
            timeout=1800,
            pause=0.02,
        )
        if "copied" not in text:
            print(f"dd did not report success: {text[-300:]}", file=sys.stderr)
            return 5

        print("reading the partition back", flush=True)
        blocks = image.stat().st_size // (1 << 20)
        text = self.command(
            f"dd if={PARTITION} bs=1M count={blocks} 2>/dev/null | sha256sum",
            timeout=1800,
            pause=0.02,
        )
        if expected not in text:
            print(
                "read-back does not match the image: a dirty page was flushed "
                "over it, or the write did not land. Not rebooting.",
                file=sys.stderr,
            )
            return 6

        print("read-back matches; rebooting into the new image", flush=True)
        self.command("sync; reboot", timeout=30)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="megrez_flash_rootfs")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--url", required=True, help="HTTP prefix serving the image")
    parser.add_argument("--log", default="/tmp/board2.log")
    parser.add_argument("--fifo", default="/tmp/board2.cmd")
    values = parser.parse_args()
    if not values.url.endswith("/"):
        values.url += "/"
    return Flash(values.log, values.fifo).run(values.image, values.url)


if __name__ == "__main__":
    raise SystemExit(main())
