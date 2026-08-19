#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import select
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
WAYLAND = TOOLS / "wayland"
SOCK_PATH = Path("/tmp/wayland-demo.sock")
MARKERS = (
    "client: registry globals received",
    "compositor: received shm pool",
    "compositor: rendered buffer to /dev/fb0",
    "client: buffer committed and acknowledged",
)


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
                    "cc",
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

            SOCK_PATH.unlink(missing_ok=True)
            process = subprocess.Popen(
                [str(demo)],
                cwd=WAYLAND,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                start_new_session=True,
            )
            transcript = bytearray()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and MARKERS[-1].encode() not in transcript:
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
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                SOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
