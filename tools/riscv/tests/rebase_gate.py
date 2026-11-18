#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the desktop image in QEMU and assert the interfaces the rebase lost.

Each check below is a defect that the 2026-11-13 rebase introduced and that was
only found on the board, one power cycle at a time:

* `/dev/ttyS0` -- the char registry never created devtmpfs nodes (upstream
  `28abe1d5a` dropped its activation call), so `StandardInput=tty` units failed
  with 208/STDIN.
* `/dev/fb0` and `/sys/class/graphics/fb0` -- Xorg's fbdev driver refuses a
  framebuffer without the class-device link (`947f5386e`).
* `/dev/input/event*` -- the boot HID devices were registered on their first
  report, which the desktop gate waits for before starting X (`e220093b6`).
* journald active -- it needs `/dev/kmsg` and `SO_TIMESTAMP` (`ad34a4ea4`).
* `/proc/sysrq-trigger` -- `17d522210` added it and a later rewrite of
  `procfs/mod.rs` dropped the registration, leaving `sysrq.rs` compiled into
  nothing. It is the only reset that needs neither a working filesystem nor an
  executable.
* `run-lock.mount` / `tmp.mount` active -- the mount table has to notify its
  pollers (`d2709e89c`).
* `ASTERINAS_DESKTOP_BOOT_READY` -- the whole desktop chain, including the
  readiness markers that `<6>` used to hide.

It also probes a path that cannot exist and requires the red branch, so a
broken check channel cannot pass the suite green.

Run it after every rebase; a red line means something was dropped again.

    tools/riscv/tests/rebase_gate.py [--keep]

Needs `qemu-system-riscv64`, a built `target/osdk/asterinas/asterinas-osdk-bin.Image`,
`/tmp/qemu-stage1.cpio` (see `tools/riscv/debian/rootfs/build_stage1.sh`) and
`/tmp/virt-fb.dtb` (a `virt` DTB with a `simple-framebuffer` node).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
KERNEL = REPO / "target/osdk/asterinas/asterinas-osdk-bin.Image"
INITRD = Path("/tmp/qemu-stage1.cpio")
ROOTFS = REPO / "target/debian-riscv/browser-web/rootfs/debian-root.ext2"
DTB = Path("/tmp/virt-fb.dtb")
SOCKET = "/tmp/rebase-gate.sock"
LOG = "/tmp/rebase-gate.log"
BOOT_TIMEOUT = 240
ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")

# The debug console echoes every line it is sent, so a marker written
# literally into the command would be found in the echo and every check would
# pass without the shell ever evaluating it. Build the marker from a variable:
# the echoed line shows `${M}_ok`, and only the guest's real output shows
# `CHECK_ok`. The negative probe below is what proved this was happening.
MARK = "M=CHECK; "

CHECKS = (
    ("serial node", "[ -c /dev/ttyS0 ] && echo ${M}_ok || echo ${M}_fail"),
    ("framebuffer node", "[ -c /dev/fb0 ] && echo ${M}_ok || echo ${M}_fail"),
    ("fb sysfs class", "[ -e /sys/class/graphics/fb0 ] && echo ${M}_ok || echo ${M}_fail"),
    ("evdev nodes", "[ \"$(ls /dev/input/event* 2>/dev/null | wc -l)\" -ge 2 ] && echo ${M}_ok || echo ${M}_fail"),
    ("journald", "systemctl is-active systemd-journald >/dev/null && echo ${M}_ok || echo ${M}_fail"),
    ("sysrq trigger", "[ -w /proc/sysrq-trigger ] && echo ${M}_ok || echo ${M}_fail"),
    ("run-lock.mount", "systemctl is-active run-lock.mount >/dev/null && echo ${M}_ok || echo ${M}_fail"),
    ("tmp.mount", "systemctl is-active tmp.mount >/dev/null && echo ${M}_ok || echo ${M}_fail"),
)

# A path that cannot exist. The red branch has to come back from the guest, or
# the harness itself is broken.
NEGATIVE_PROBE = "[ -w /proc/.rebase-gate-absent ] && echo ${M}_ok || echo ${M}_fail"


def check_arch() -> bool:
    """Refuse to boot if the image at KERNEL is not a riscv64 build.

    The OSDK writes every build into the same `target/osdk/asterinas` folder,
    so an x86 build silently replaces the riscv image and the gate then fails
    for a reason that has nothing to do with the kernel. The bundle manifest
    next to the image records the architecture it was built for.
    """
    bundle = KERNEL.parent / "bundle.toml"
    try:
        text = bundle.read_text()
    except OSError:
        print(f"missing {bundle}; run the riscv build first", file=sys.stderr)
        return False
    match = re.search(r'^arch\s*=\s*"([^"]+)"', text, re.MULTILINE)
    arch = match.group(1) if match else "unknown"
    if arch != "riscv64":
        print(f"image was built for {arch}, not riscv64: rebuild with "
              f"--target-arch riscv64 --scheme riscv", file=sys.stderr)
        return False
    return True


def boot():
    for path in (SOCKET, LOG):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    cmd = [
        "qemu-system-riscv64", "-machine", "virt", "-cpu", "rv64",
        "-smp", "2", "-m", "2G", "-display", "none", "-snapshot", "-dtb", str(DTB),
        "-chardev", f"socket,id=ser0,path={SOCKET},server=on,wait=off,logfile={LOG}",
        "-serial", "chardev:ser0",
        "-kernel", str(KERNEL), "-initrd", str(INITRD),
        "-drive", f"file={ROOTFS},format=raw,if=none,id=rootdisk",
        "-device", "virtio-blk-device,drive=rootdisk",
        "-device", "virtio-keyboard-device", "-device", "virtio-tablet-device",
        "-append",
        "console=ttyS0 loglevel=warn init=/init systemd.show_status=yes "
        "systemd.log_target=console "
        "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
        "-- --root-init=systemd --debug-console=isolated-root",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        time.sleep(5)
        try:
            text = Path(LOG).read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            continue
        if "ASTERINAS_DESKTOP_BOOT_READY" in text:
            return proc, True
        if "Uncaught panic" in text:
            return proc, False
    return proc, False


def ask(cmd: str) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET)
    sock.settimeout(0.3)

    def drain(seconds):
        end = time.time() + seconds
        out = b""
        while time.time() < end:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return out

    drain(0.4)
    sock.sendall((cmd + "\n").encode())
    time.sleep(2.5)
    text = drain(6.0).decode("utf-8", "replace")
    sock.close()
    return ANSI.sub("", text).replace("\r", "")


def main() -> int:
    for path in (KERNEL, INITRD, ROOTFS, DTB):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2
    if not check_arch():
        return 2

    proc, ready = boot()
    failures = []
    try:
        if not ready:
            log = Path(LOG).read_bytes().decode("utf-8", "replace") if Path(LOG).exists() else ""
            panic = "Uncaught panic" in log
            print(f"desktop: FAIL ({'panic' if panic else 'no BOOT_READY within '
                                       f'{BOOT_TIMEOUT}s'})")
            return 1
        print("desktop: PASS (ASTERINAS_DESKTOP_BOOT_READY)")

        for name, cmd in CHECKS:
            reply = ask(MARK + cmd)
            ok = "CHECK_ok" in reply
            print(f"{name}: {'PASS' if ok else 'FAIL'}")
            if not ok:
                failures.append(name)

        # A gate that only ever prints PASS is worthless: a garbled reply, a
        # stale socket or a shell that echoes the command back would read as
        # green. Probe a path that cannot exist and require the red branch.
        reply = ask(MARK + NEGATIVE_PROBE)
        if "CHECK_fail" in reply and "CHECK_ok" not in reply:
            print("negative probe: PASS (an absent node reports fail)")
        else:
            print("negative probe: FAIL (the check channel does not discriminate)")
            failures.append("negative probe")
    finally:
        proc.kill()
        proc.wait()

    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
