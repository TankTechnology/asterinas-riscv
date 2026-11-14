#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Log the board's own address into the campus portal over the serial console.

The portal signs a login for the address the request comes from, so the
request has to be made on the board. The board has no secrets and no
NetworkManager, so this tool borrows the host's stored credentials for the
duration of one command and never writes them anywhere:

  * it stops the serial bridge first, because the bridge appends everything it
    reads to a log file -- the echo of the command line would put the password
    on disk;
  * it opens /dev/ttyUSB0 itself, types the command with `SRUN_USER` /
    `SRUN_PASS` in the environment (the board script reads those before it
    looks for nmcli), and keeps the port until the shell answers;
  * the transcript it saves is redacted, and it restarts the bridge afterwards.

    sudo tools/riscv/board/portal_env_login.py --board-ip 192.0.2.10 \\
        --bridge-log /tmp/megrez2.log --fifo /tmp/megrez.cmd

`--board-ip` is site configuration; the default is a documentation address on
purpose. Exit status: 0 verified online, 1 the portal refused, 2 setup failed.
"""

from __future__ import annotations

import argparse
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time

DEVICE = "/dev/ttyUSB0"


def credentials(profile: str) -> tuple[str, str]:
    """Reads the campus credentials from NetworkManager, without printing them."""

    def field(name: str) -> str:
        result = subprocess.run(
            ["nmcli", "-s", "-g", name, "connection", "show", profile],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip()

    identity = field("802-1x.identity")
    password = field("802-1x.password")
    if not identity or not password:
        sys.exit(f"no credentials in NetworkManager profile {profile!r}")
    return identity, password


def stop_bridge() -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = open(f"/proc/{entry}/cmdline", "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        if "megrez_console.py" in cmdline and " bridge " in cmdline:
            pids.append(int(entry))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.5)
    return pids


def start_bridge(log: str, fifo: str) -> None:
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    subprocess.Popen(
        [sys.executable, os.path.join(repo, "tools/riscv/board/megrez_console.py"),
         "bridge", DEVICE, "--log", log, "--fifo", fifo],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-ip", required=True)
    parser.add_argument("--profile", default="ZGCLab")
    parser.add_argument("--ac-id", default="1")
    parser.add_argument("--bridge-log", default="/tmp/megrez2.log")
    parser.add_argument("--fifo", default="/tmp/megrez.cmd")
    parser.add_argument("--wait", type=float, default=45.0)
    args = parser.parse_args()

    identity, password = credentials(args.profile)
    stopped = stop_bridge()
    transcript = ""
    try:
        fd = os.open(DEVICE, os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(fd, b"\n")
            time.sleep(1.0)
            command = (
                f"SRUN_USER={shlex.quote(identity)} SRUN_PASS={shlex.quote(password)} "
                f"python3 /root/srun_board.py --expect-ip {args.board_ip} "
                f"--ac-id {args.ac_id}\n"
            )
            for char in command:
                os.write(fd, char.encode())
                time.sleep(0.01)
            deadline = time.time() + args.wait
            chunks = []
            while time.time() < deadline:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                chunks.append(os.read(fd, 4096))
                text = b"".join(chunks).decode("utf-8", "replace")
                if "login:" in text and text.rstrip().endswith("#"):
                    break
            transcript = b"".join(chunks).decode("utf-8", "replace")
        finally:
            os.close(fd)
    finally:
        if stopped:
            start_bridge(args.bridge_log, args.fifo)

    redacted = transcript.replace(password, "***").replace(identity, "***")
    redacted = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", redacted)
    os.makedirs("/tmp/tp", exist_ok=True)
    with open("/tmp/tp/portal-login.log", "w", encoding="utf-8") as handle:
        handle.write(redacted)

    print(redacted[-800:])
    return 0 if re.search(r"login: (ok|login_ok)", redacted) else 1


if __name__ == "__main__":
    sys.exit(main())
