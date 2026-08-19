#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import fcntl
import os
import select
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
WAYLAND = TOOLS / "wayland"
SOCK_PATH = Path("/tmp/wayland-demo.sock")
SOCK_LOCK_PATH = Path("/tmp/wayland-demo.sock.lock")
CC = shutil.which("cc")
MARKERS = (
    "client: registry globals received",
    "compositor: received shm pool",
    "compositor: rendered buffer to /dev/fb0",
    "client: buffer committed and acknowledged",
)


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _signal_process_group(pgid: int, signal_number: int) -> None:
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        pass


def _wait_for_process_group(process: subprocess.Popen[bytes], pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        process.poll()
        time.sleep(0.01)
    return True


def _cleanup_process_group(process: subprocess.Popen[bytes], pgid: int) -> None:
    _signal_process_group(pgid, signal.SIGTERM)
    if not _wait_for_process_group(process, pgid, 1):
        _signal_process_group(pgid, signal.SIGKILL)
        _wait_for_process_group(process, pgid, 1)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _signal_process_group(pgid, signal.SIGKILL)
        process.wait()


@unittest.skipUnless(CC, "requires host cc")
class WaylandDemoTests(unittest.TestCase):
    def test_host_handshake_reaches_all_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            demo = tmpdir_path / "wayland-demo"
            host_open = tmpdir_path / "host_open.c"
            host_open.write_text(
                """#include <errno.h>
#include <stdarg.h>

int host_open(const char *path, int flags, ...) {
    (void)path;
    (void)flags;
    errno = ENOENT;
    return -1;
}
"""
            )
            subprocess.run(
                [
                    CC,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Dopen=host_open",
                    "-o",
                    str(demo),
                    str(host_open),
                    str(WAYLAND / "wire.c"),
                    str(WAYLAND / "compositor.c"),
                    str(WAYLAND / "client.c"),
                ],
                check=True,
                cwd=WAYLAND,
            )

            lock_fd = os.open(SOCK_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                if SOCK_PATH.exists():
                    self.fail(f"refusing to remove pre-existing socket: {SOCK_PATH}")

                process = subprocess.Popen(
                    [str(demo)],
                    cwd=WAYLAND,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=False,
                    start_new_session=True,
                )
                pgid = process.pid
                socket_identity: tuple[int, int] | None = None
                transcript = bytearray()
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and MARKERS[-1].encode() not in transcript:
                        if socket_identity is None and SOCK_PATH.exists():
                            socket_stat = SOCK_PATH.stat()
                            socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
                        if process.stdout is None:
                            self.fail("wayland demo stdout was not captured")
                        ready, _, _ = select.select([process.stdout], [], [], deadline - time.monotonic())
                        if ready:
                            chunk = os.read(process.stdout.fileno(), 4096)
                            if not chunk:
                                break
                            transcript.extend(chunk)
                            if b"bad global size" in transcript:
                                break
                        elif process.poll() is not None:
                            break

                    output = transcript.decode(errors="replace")
                    self.assertNotIn("bad global size", output, output)
                    for marker in MARKERS:
                        self.assertIn(marker, output, output)
                finally:
                    _cleanup_process_group(process, pgid)
                    if process.stdout is not None:
                        process.stdout.close()
                    if socket_identity is not None and SOCK_PATH.exists():
                        socket_stat = SOCK_PATH.stat()
                        if (socket_stat.st_dev, socket_stat.st_ino) == socket_identity:
                            SOCK_PATH.unlink()
            finally:
                os.close(lock_fd)


if __name__ == "__main__":
    unittest.main()
