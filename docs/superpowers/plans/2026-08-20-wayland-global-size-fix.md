# Wayland Registry Message Size Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the RISC-V Wayland demo's registry-event header decoding and prove the complete shared-memory render path in QEMU.

**Architecture:** Keep the production change to the client registry receive loop: decode the Wayland message size from the header's high 16 bits, matching the existing writer and compositor. Add one host integration test that compiles and runs the real client, compositor, and wire codec, then rebuild the RISC-V initramfs and verify its serial milestones and RGB framebuffer output in QEMU.

**Tech Stack:** C11/POSIX sockets, Python `unittest`, RISC-V GNU cross compiler, Docker, U-Boot, QEMU `riscv64`, Asterinas Sv39.

---

## File map

- Create `tools/riscv/tests/test_wayland_demo.py`: host integration regression test for the production Wayland demo handshake.
- Modify `tools/riscv/wayland/client.c`: read the registry event size from the high 16 bits of the Wayland header.
- Generate ignored evidence under `target/qemu-uboot/wayland-fixed/` and `target/demo/wayland-fixed/`; do not commit these build artifacts.

### Task 1: Reproduce and fix the registry header bug with TDD

**Files:**
- Create: `tools/riscv/tests/test_wayland_demo.py`
- Modify: `tools/riscv/wayland/client.c:105-119`
- Test: `tools/riscv/tests/test_wayland_demo.py`

- [ ] **Step 1: Add the failing host integration test**

Create `tools/riscv/tests/test_wayland_demo.py` with this content:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


WAYLAND_DIR = Path(__file__).resolve().parents[1] / "wayland"
SOCKET_PATH = Path("/tmp/wayland-demo.sock")
HANDSHAKE_MARKERS = (
    "client: registry globals received",
    "compositor: received shm pool",
    "compositor: rendered buffer to /dev/fb0",
    "client: buffer committed and acknowledged",
)


class WaylandDemoTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "a host C compiler is required")
    def test_production_demo_completes_shared_memory_handshake(self) -> None:
        SOCKET_PATH.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "wayland-demo"
            subprocess.run(
                [
                    shutil.which("cc"),
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-o",
                    str(binary),
                    str(WAYLAND_DIR / "wire.c"),
                    str(WAYLAND_DIR / "compositor.c"),
                    str(WAYLAND_DIR / "client.c"),
                ],
                check=True,
            )

            process = subprocess.Popen(
                [str(binary)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            output = bytearray()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if chunk:
                            output.extend(chunk)
                    if HANDSHAKE_MARKERS[-1].encode() in output:
                        break
                    if process.poll() is not None:
                        break
            finally:
                selector.close()
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                if process.stdout is not None:
                    output.extend(process.stdout.read())
                SOCKET_PATH.unlink(missing_ok=True)

            transcript = output.decode(errors="replace")
            self.assertNotIn("bad global size", transcript)
            for marker in HANDSHAKE_MARKERS:
                with self.subTest(marker=marker):
                    self.assertIn(marker, transcript)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest tools/riscv/tests/test_wayland_demo.py -v
```

Expected: `FAIL`; the transcript contains `bad global size`, so
`assertNotIn("bad global size", transcript)` fails before any shared-memory
render marker is reached.

- [ ] **Step 3: Make the minimal production change**

In `tools/riscv/wayland/client.c`, replace the incorrect size extraction:

```c
uint16_t size = (uint16_t)(sz_op & 0xffffu);
```

with:

```c
uint16_t size = (uint16_t)(sz_op >> 16);
```

Do not change the stream parser, wire encoder, message pacing, or socket code.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
python3 -m unittest tools/riscv/tests/test_wayland_demo.py -v
```

Expected: `OK`; all four handshake markers are present and `bad global size`
is absent.

- [ ] **Step 5: Run the full RISC-V tooling test suite**

Run:

```bash
PYTHONPATH=tools/riscv \
  python3 -m unittest discover -s tools/riscv/tests -p 'test_*.py'
git diff --check
```

Expected: all discovered tests pass, with only documented environment-dependent
skips; `git diff --check` produces no output.

- [ ] **Step 6: Commit the tested source change**

Run:

```bash
git add tools/riscv/tests/test_wayland_demo.py tools/riscv/wayland/client.c
git commit -m "fix(riscv): decode Wayland registry message size"
```

Expected: one commit containing only the regression test and the one-line
production fix.

### Task 2: Rebuild and verify the RISC-V Wayland demo in QEMU

**Files:**
- Generate: `target/qemu-uboot/initramfs-wayland-fixed.cpio.gz`
- Generate: `target/qemu-uboot/wayland-fixed/boot.ext4`
- Generate: `target/demo/wayland-fixed/serial.log`
- Generate: `target/demo/wayland-fixed/early.ppm`
- Generate: `target/demo/wayland-fixed/early.png`

- [ ] **Step 1: Rebuild the static RISC-V initramfs**

The current `asterinas-env:uboot-sim` image contains the cross compiler but not
the RISC-V glibc development headers. Install those headers only inside the
disposable build container and run the existing build script:

```bash
docker run --rm \
  -v "$PWD":/root/asterinas \
  -w /root/asterinas \
  asterinas-env:uboot-sim \
  bash -lc 'apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libc6-dev-riscv64-cross && \
    bash tools/riscv/wayland/build_wayland.sh \
      /root/asterinas/target/qemu-uboot/initramfs-wayland-fixed.cpio.gz'
file target/wayland/init
```

Expected: the build reports `built ...initramfs-wayland-fixed.cpio.gz`, and
`file` identifies `target/wayland/init` as a statically linked 64-bit RISC-V
executable.

- [ ] **Step 2: Prepare an independent U-Boot boot disk**

Run:

```bash
docker run --rm \
  -v "$PWD":/root/asterinas \
  -w /root/asterinas \
  -e ASTERINAS_RISCV_BOOTI=/root/asterinas/target/osdk/aster-kernel-osdk-bin.Image \
  -e ASTERINAS_INITRAMFS=/root/asterinas/target/qemu-uboot/initramfs-wayland-fixed.cpio.gz \
  -e QEMU_UBOOT_PROFILE=generic-sv39 \
  -e QEMU_UBOOT_OUT_DIR=/root/asterinas/target/qemu-uboot/wayland-fixed \
  -e QEMU_UBOOT_CACHE_DIR=/root/asterinas/target/qemu-uboot/cache \
  -e QEMU_UBOOT_BUILD_DIR=/root/asterinas/target/qemu-uboot/cache/u-boot-build \
  asterinas-env:uboot-sim \
  bash tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

Expected: `prepared=/root/asterinas/target/qemu-uboot/wayland-fixed`; the
existing `target/qemu-uboot/current/boot.ext4` remains unchanged.

- [ ] **Step 3: Boot headlessly and capture the framebuffer before `/init` exits**

Run:

```bash
mkdir -p target/demo/wayland-fixed
docker run --rm -i \
  -v "$PWD":/root/asterinas \
  -w /root/asterinas \
  asterinas-env:uboot-sim \
  timeout -s INT 15 python3 - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "tools/riscv")
import qemu_desktop_boot as demo

demo.BOOT_DISK = Path("/root/asterinas/target/qemu-uboot/wayland-fixed/boot.ext4")
demo.SERIAL_LOG = Path("/root/asterinas/target/demo/wayland-fixed/serial.log")
demo.SCREENSHOT = Path("/root/asterinas/target/demo/wayland-fixed/final.ppm")
raise SystemExit(demo.main())
PY
test "$?" -eq 124
docker run --rm -v "$PWD":/root/asterinas asterinas-env:uboot-sim \
  chown -R 1000:1000 /root/asterinas/target/demo/wayland-fixed
```

Expected: the driver reports the userspace marker and writes
`target/demo/wayland-fixed/early.ppm`. Exit 124 is expected because the timeout
interrupts the driver's deliberate 150-second desktop-settle sleep after the
early screendump.

- [ ] **Step 4: Verify serial milestones and reject the original failure**

Run:

```bash
python3 - <<'PY'
from pathlib import Path

text = Path("target/demo/wayland-fixed/serial.log").read_bytes().replace(b"\0", b"")
required = (
    b"client: registry globals received",
    b"compositor: received shm pool",
    b"compositor: rendered buffer to /dev/fb0",
    b"client: buffer committed and acknowledged",
)
for marker in required:
    assert marker in text, marker
for rejected in (b"bad global size", b"Uncaught panic", b"kernel panic"):
    assert rejected not in text, rejected
print("Wayland serial milestones: PASS")
PY
```

Expected: `Wayland serial milestones: PASS`.

- [ ] **Step 5: Verify the three framebuffer bands**

Run:

```bash
python3 - <<'PY'
from pathlib import Path

path = Path("target/demo/wayland-fixed/early.ppm")
magic, dimensions, maximum, pixels = path.read_bytes().split(b"\n", 3)
width, height = map(int, dimensions.split())
assert (magic, width, height, maximum) == (b"P6", 1280, 1024, b"255")

def pixel(x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 3
    return tuple(pixels[offset : offset + 3])

samples = (pixel(640, 100), pixel(640, 512), pixel(640, 900))
assert samples == ((255, 0, 0), (0, 255, 0), (0, 0, 255)), samples
print("Wayland RGB bands: PASS", samples)
PY
convert target/demo/wayland-fixed/early.ppm \
  target/demo/wayland-fixed/early.png
```

Expected: `Wayland RGB bands: PASS ((255, 0, 0), (0, 255, 0), (0, 0, 255))`
and a 1280×1024 PNG containing red, green, and blue horizontal bands.

- [ ] **Step 6: Confirm the repository handoff state**

Run:

```bash
git status --short
git log -3 --oneline --decorate
```

Expected: no unexpected tracked modifications; ignored QEMU evidence remains
under `target/`; the design commit and source-fix commit are at the branch tip.
