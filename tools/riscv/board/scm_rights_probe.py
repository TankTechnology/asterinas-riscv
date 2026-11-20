#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Check that an unprivileged process may pass a socket with SCM_RIGHTS.

Gecko hands a new child process its IPC channel by sending one end of a
UNIX socket pair over another UNIX socket, as the unprivileged user the
browser runs as. When the kernel required `CAP_SYS_ADMIN` for that transfer,
every child launch failed with EPERM -- the browser window appeared, no
content process ever did, and nothing in userspace said why:

    WARNING: Failed to launch socket subprocess @FSC::SFNS::Send
    WARNING: Failed to launch tab subprocess

The kernel now follows Linux and allows the transfer for any caller
(`unix: let an ordinary user pass a socket descriptor with SCM_RIGHTS`).
This probe is the regression guard: run it as root and again as the user a
desktop session actually uses.

    scm_rights_probe.py                 # as the current user
    scm_rights_probe.py --user 1000     # drop to that uid first
    scm_rights_probe.py --user 1000 --gid 1000

Exit status is 0 when the descriptor arrives intact, 1 otherwise, so the
board's shell can use it directly in a gate. Both outcomes print a single
line: `SCM_OK` or `SCM_FAIL <errno>`.
"""

from __future__ import annotations

import argparse
import array
import os
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user",
        type=int,
        help="drop to this uid before probing (the desktop user is 1000)",
    )
    parser.add_argument("--gid", type=int, help="drop to this gid as well")
    args = parser.parse_args()

    if args.user is not None:
        if os.geteuid() != 0:
            sys.exit("--user needs root to drop privileges")
        if args.gid is not None:
            os.setgroups([])
            os.setgid(args.gid)
        os.setuid(args.user)

    sender, receiver = socket.socketpair()
    carried, carried_peer = socket.socketpair()
    try:
        payload = array.array("i", [carried.fileno()])
        sender.sendmsg([b"x"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, payload)])
    except OSError as error:
        print("SCM_FAIL %d" % error.errno, flush=True)
        return 1

    data, ancillary, _flags, _address = receiver.recvmsg(1, socket.CMSG_SPACE(4))
    descriptors = [entry for entry in ancillary if entry[0] == socket.SOL_SOCKET]
    if data != b"x" or not descriptors:
        print("SCM_FAIL no-descriptor", flush=True)
        return 1
    print("SCM_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
