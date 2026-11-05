#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bring the Megrez board back to a usable desktop in one command.

Written for the case where the board has been power-cycled (or needs to be) and
someone has to redo the whole bring-up by hand: wait for U-Boot, boot the
chosen p2 kernel with the campus network address, wait for the desktop, set the
clock from the build host, authenticate the campus portal, and check that DNS,
HTTPS and the framebuffer all agree.

    megrez_recover.py                 # boot /root/kernel-rel.img and network it
    megrez_recover.py --kernel /root/kernel-sig.img --skip-portal
    megrez_recover.py --credentials-file /root/.srun   # "<user> <pass>"

The portal credentials are never stored in the repository; pass them on the
command line or point at a root-only file on the host.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "board"))

from megrez_console import Console  # noqa: E402


# Board addressing is site configuration. Read it from the environment and fall
# back to the RFC 5737 documentation addresses, so the repository never carries
# a real public address (see xaj_vibenote.md, section 7).
def _site_address(variable: str, documentation: str) -> str:
    value = os.environ.get(variable)
    if value:
        return value
    print(
        f"warning: {variable} is not set; using the documentation address "
        f"{documentation} (set it to the board's address)",
        file=sys.stderr,
    )
    return documentation


BOARD_IP = _site_address("BOARD_IP", "192.0.2.10")
BOARD_GW = _site_address("BOARD_GW", "192.0.2.1")
PORTAL_IP = _site_address("PORTAL_IP", "192.0.2.20")
PORTAL_HOST = os.environ.get("PORTAL_HOST", "auth.zgclab.edu.cn")


def wait_for(con: Console, pattern: str, timeout: float, label: str) -> str:
    deadline = time.time() + timeout
    text = ""
    offset = os.path.getsize(con.log)
    while time.time() < deadline:
        time.sleep(2)
        chunk, offset = con.read_since(offset)
        text += chunk
        if re.search(pattern, text):
            return text
    raise SystemExit(f"timed out waiting for {label}\n{text[-800:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", default="", help="path inside p2; empty runs the stored bootcmd")
    parser.add_argument("--credentials", help="'<user> <pass>' for the campus portal")
    parser.add_argument("--credentials-file", type=Path, help="root-only file with '<user> <pass>'")
    parser.add_argument("--skip-portal", action="store_true")
    parser.add_argument("--log", default="/tmp/board.log")
    parser.add_argument("--fifo", default="/tmp/board.cmd")
    args = parser.parse_args()

    con = Console(args.log, args.fifo)
    append = (
        f"console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2 "
        f"asterinas.net=eic7700-rj45,{BOARD_IP}/24,{BOARD_GW} "
        f"-- --root-init=systemd --debug-console=isolated-root"
    )

    # The board's U-Boot environment now carries the whole boot sequence and
    # the arguments that go with it (see `megrez_boot_cmd.txt`), so a plain
    # power-on already boots into a writable root with the campus address.
    # This script therefore only has to get a kernel running when someone has
    # interrupted that path: at the prompt it types `boot`, during the
    # countdown it stops the countdown first, and only when neither appears
    # does it load the kernel by hand.
    print("[1/5] looking for U-Boot", flush=True)
    text = wait_for(con, r"Hit any key to stop autoboot|(?m)^=>\s*$", 300, "U-Boot")
    if "Hit any key" in text and not re.search(r"(?m)^=>\s*$", text):
        for _ in range(8):
            con.send_line(" ")
            time.sleep(0.6)
        time.sleep(2)

    if args.kernel == "":
        print("[2/5] running the stored bootcmd", flush=True)
        con.type_line("boot")
    else:
        print(f"[2/5] booting {args.kernel}", flush=True)
        steps = [
            ("ext4load mmc 1:1 0x88300000 stage1-daily.cpio", r"bytes read|Failed"),
            ("setenv initrd_size ${filesize}", r"(?m)^=>"),
            ("ext4load mmc 1:1 0x88000000 megrez-daily.dtb", r"bytes read|Failed"),
            (f"ext4load mmc 1:2 0x80200000 {args.kernel}", r"bytes read|Failed"),
            (f"setenv bootargs {append}", r"(?m)^=>"),
        ]
        for command, pattern in steps:
            con.type_line(command)
            out = wait_for(con, pattern, 120, command)
            if "Failed" in out.split(command)[-1]:
                raise SystemExit(f"rejected: {command}")
        con.type_line("booti 0x80200000 0x88300000:${initrd_size} 0x88000000")
    wait_for(con, r"ASTERINAS_DEBUG_CONSOLE_READY", 400, "debug console")

    print("[3/5] setting the clock from this host", flush=True)
    epoch = int(time.time())
    text, ok = con.run(f"date -s @{epoch}", 60)
    print("   ", text.strip().splitlines()[-1] if text.strip() else "(no output)")

    if not args.skip_portal:
        credentials = args.credentials
        if credentials is None and args.credentials_file:
            credentials = args.credentials_file.read_text().strip()
        if credentials:
            print("[4/5] authenticating the campus portal", flush=True)
            host_line = f"{PORTAL_IP} {PORTAL_HOST}"
            con.run(f"grep -q {PORTAL_HOST} /etc/hosts || echo '{host_line}' >> /etc/hosts", 60)
            text, ok = con.run(f"python3 /root/srun.py {credentials}", 120)
            match = re.search(r'"suc_msg":"([^"]*)"', text)
            print("   ", match.group(1) if match else "no portal reply")
        else:
            print("[4/5] portal skipped: no credentials given", flush=True)
    else:
        print("[4/5] portal skipped", flush=True)

    print("[5/5] checking the desktop and the network", flush=True)
    checks = [
        ("desktop", "systemctl is-active asterinas-desktop-m5 asterinas-browser-web"),
        ("dns", "getent hosts www.baidu.com | head -1"),
        ("https", "curl -m 10 -so /dev/null -w '%{http_code}' https://www.baidu.com/"),
        ("wallpaper", "grep '^wallpaper' /home/asterinas/.config/pcmanfm/Asterinas/desktop-items-0.conf"),
    ]
    for label, command in checks:
        text, ok = con.run(command, 60)
        body = " / ".join(line.strip() for line in text.splitlines() if line.strip() and "root@" not in line)
        print(f"    {label:9s} {body[:120]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
