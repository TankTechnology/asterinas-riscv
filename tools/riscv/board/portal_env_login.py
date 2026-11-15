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

`--browser` drives the board's own Firefox instead (`/root/portal_browser_login.py`,
Marionette on `127.0.0.1:2828`) and types the credentials into its stdin once it
prints `READY`. Use it when the srun protocol answers `sign_error`, which is what
happens when the portal still believes the address is online; the portal's own
JavaScript then does the signing.

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


def bridge_pids() -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = open(f"/proc/{entry}/cmdline", "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        # /proc/<pid>/cmdline separates arguments with NUL, not spaces, so a
        # substring test for " bridge " never matches and the bridge keeps
        # reading the port. Split it instead; missing this once put the
        # password in the bridge log.
        argv = cmdline.split("\0")
        if any("megrez_console.py" in arg for arg in argv) and "bridge" in argv:
            pids.append(int(entry))
    return pids


def stop_bridge() -> list[int]:
    pids = bridge_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.5)
    for pid in bridge_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.3)
    return pids


def start_bridge(log: str, fifo: str) -> None:
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    # A fifo a previous run left behind as root stops the next bridge as the
    # invoking user ("Operation not permitted" on unlink). Remove it here,
    # while the privileges that can still do so are in hand.
    try:
        os.unlink(fifo)
    except OSError:
        pass
    # Run the bridge as whoever invoked sudo. Started as root it cannot even
    # open the log: `fs.protected_regular=2` makes /tmp/megrez2.log -- owned by
    # the invoking user, in a sticky directory -- refuse root's append, and the
    # bridge dies before it reads a byte. The supplementary groups have to come
    # along too: `/dev/ttyUSB0` is `root:uucp`, and a process without `uucp`
    # gets EACCES on open.
    as_user = {}
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        import pwd

        as_user = {
            "user": int(uid),
            "group": int(gid),
            "extra_groups": os.getgrouplist(pwd.getpwuid(int(uid)).pw_name, int(gid)),
        }
    errors = "/tmp/tp/bridge-restart.log"
    os.makedirs(os.path.dirname(errors), exist_ok=True)
    with open(errors, "wb") as err:
        subprocess.Popen(
            [sys.executable, os.path.join(repo, "tools/riscv/board/megrez_console.py"),
             "bridge", DEVICE, "--log", log, "--fifo", fifo],
            stdout=subprocess.DEVNULL, stderr=err, start_new_session=True, **as_user,
        )
    # A bridge that loses the race for the port exits at once and leaves the
    # console silent, which looks exactly like a board that stopped talking.
    # Say so here instead of leaving the next command to time out.
    for _ in range(15):
        if bridge_pids():
            break
        time.sleep(0.2)
    else:
        print("warning: the serial bridge did not come back; start it by hand",
              file=sys.stderr)
    # This tool runs under sudo, so the bridge it leaves behind recreates the
    # fifo as root, and the next bridge started as the normal user cannot
    # unlink it ("Operation not permitted") and refuses to start. Hand the
    # fifo back to whoever invoked sudo.
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        try:
            os.chown(fifo, int(uid), int(gid))
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-ip", required=True)
    parser.add_argument("--profile", default="ZGCLab")
    parser.add_argument("--ac-id", default="1")
    parser.add_argument("--bridge-log", default="/tmp/megrez2.log")
    parser.add_argument("--fifo", default="/tmp/megrez.cmd")
    parser.add_argument("--wait", type=float, default=45.0)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="drive the board's Firefox via /root/portal_browser_login.py instead of srun",
    )
    parser.add_argument("--logout", action="store_true", help="log the address out first")
    args = parser.parse_args()

    identity, password = credentials(args.profile)
    stopped = stop_bridge()
    if bridge_pids():
        # Anything still attached to the port would echo the command line into
        # its log. Refuse rather than take that risk.
        sys.exit("serial bridge still running; refusing to type credentials")
    transcript = ""
    try:
        fd = os.open(DEVICE, os.O_RDWR | os.O_NOCTTY)
        try:
            # The portal answers `challenge_expire_error` to a board whose
            # clock drifted more than its window allows, which is what a board
            # without a working RTC looks like after every boot. Setting the
            # time first costs one line and removes that whole failure mode.
            host_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            clock = f"date -u -s '{host_utc}'\n"
            for char in clock:
                os.write(fd, char.encode())
                time.sleep(0.01)
            time.sleep(1.5)
            os.write(fd, b"\n")
            time.sleep(0.5)

            def type_text(text: str) -> None:
                for char in text:
                    os.write(fd, char.encode())
                    time.sleep(0.01)

            chunks: list[bytes] = []

            def pump(until, wait: float) -> bool:
                """Reads the port until `until(transcript)` holds or `wait` runs out."""
                deadline = time.time() + wait
                while time.time() < deadline:
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        continue
                    chunks.append(os.read(fd, 4096))
                    if until(b"".join(chunks).decode("utf-8", "replace")):
                        return True
                return False

            if args.browser:
                # The board script turns the console's echo off before it prints
                # READY, so the two lines typed below never come back through the
                # port -- and never reach a log.
                type_text("python3 /root/portal_browser_login.py\n")
                if not pump(lambda text: "READY" in text, 20.0):
                    sys.exit("the board never printed READY; credentials were not typed")
                type_text(identity + "\n")
                type_text(password + "\n")
                pump(lambda text: "BROWSER_LOGIN_DONE" in text, args.wait)
            else:
                extra = " --logout" if args.logout else ""
                command = (
                    f"SRUN_USER={shlex.quote(identity)} SRUN_PASS={shlex.quote(password)} "
                    f"python3 /root/srun_board.py --expect-ip {args.board_ip} "
                    f"--ac-id {args.ac_id}{extra}\n"
                )
                type_text(command)
                pump(
                    lambda text: "login:" in text and text.rstrip().endswith("#"),
                    args.wait,
                )
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
    if args.browser:
        # The browser path has no `login:` line to match: the portal's page
        # reports the result in the text printed by the board script, which the
        # caller confirms by reaching the address afterwards.
        return 0 if "BROWSER_LOGIN_DONE" in redacted else 1
    return 0 if re.search(r"login: (ok|login_ok)", redacted) else 1


if __name__ == "__main__":
    sys.exit(main())
