#!/usr/bin/env python3
"""Automated backspace verification: boot QEMU, wait for xterm, type test text,
send backspace, capture screenshots to verify characters are erased.

Usage:
    python3 tools/riscv/systemd/boot_backspace_test.py
"""

from __future__ import annotations

import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

REPO = Path(__file__).resolve().parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = Path("/tmp/vnc-demo/boot.ext4")
MON_SOCK = Path("/tmp/backspace-test-mon.sock")
SCREENSHOT_DIR = Path("/tmp/backspace-test-screenshots")

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x9000_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
XTERM_STARTED = b"Started XTerm terminal emulator"
GRAPHICAL_TARGET = b"Reached target Graphical Interface"
XORG_INPUT = b"Adding extended input device"

# After graphical.target + xorg input devices are up, wait for the desktop to
# settle.  xterm is started by systemd; we then send keystrokes via QEMU
# monitor's sendkey command.
SETTLE_SECONDS = 15


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("dtb-resize", "fdt resize 0x1000", "=>"),
        ("pci-probe", "pci display 0.1.0", "=>"),
        ("fb-mknode", "fdt mknode / framebuffer@40000000", "=>"),
        ("fb-compatible", 'fdt set /framebuffer@40000000 compatible "simple-framebuffer"', "=>"),
        ("fb-reg", "fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>", "=>"),
        ("fb-width", "fdt set /framebuffer@40000000 width <0x500>", "=>"),
        ("fb-height", "fdt set /framebuffer@40000000 height <0x400>", "=>"),
        ("fb-stride", "fdt set /framebuffer@40000000 stride <0x1400>", "=>"),
        ("fb-format", 'fdt set /framebuffer@40000000 format "x8r8g8b8"', "=>"),
        ("fb-status", 'fdt set /framebuffer@40000000 status "okay"', "=>"),
        ("fb-verify", "fdt print /framebuffer@40000000", "simple-framebuffer"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


class Boot:
    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.pending = bytearray()
        self.transcript = bytearray()

    def _drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for key, _ in self.sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.pending.extend(chunk)

    def read_until(self, needle: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tail = bytes(self.transcript[-800:])
                raise TimeoutError(
                    f"timed out waiting for {needle!r}; tail={tail!r}"
                )
            self._drain(min(remaining, 1.0))
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return consumed

    def send(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        self.sel.close()


def mon_connect(sock: Path) -> socket.socket | None:
    if not sock.exists():
        return None
    for _ in range(10):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(str(sock))
            time.sleep(0.3)
            s.recv(4096)  # drain banner
            return s
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.5)
    return None


def mon_cmd(sock: socket.socket, cmd: str) -> None:
    sock.sendall((cmd + "\n").encode())
    time.sleep(0.3)


def screendump(sock: Path, path: Path) -> None:
    if not sock.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock))
            s.sendall(f"screendump {path}\n".encode())
            time.sleep(2.0)
    except OSError:
        pass


def sendkeys(mon: socket.socket, keys: str) -> None:
    """Send keys via QEMU monitor sendkey. Keys use QEMU key names,
    dash-separated for combos."""
    mon_cmd(mon, f"sendkey {keys}")
    time.sleep(0.5)


def main() -> int:
    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if MON_SOCK.exists():
        MON_SOCK.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-display", "vnc=127.0.0.1:1",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "bochs-display",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-serial", "stdio",
        "-monitor", f"unix:{MON_SOCK},server,nowait",
    ]

    print("[boot] starting QEMU", flush=True)
    boot = Boot(argv)

    try:
        # ---- U-Boot phase ----
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)

        for name, text, expected in uboot_commands():
            print(f"[uboot] {name}", flush=True)
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        # ---- Wait for desktop ----
        print("[boot] waiting for /init launcher", flush=True)
        boot.read_until(INIT_MARKER, 120)
        print("[ok] /init reached (exec'ing systemd)", flush=True)

        print(f"[boot] waiting for desktop (graphical.target + xterm)", flush=True)
        deadline = time.monotonic() + 300
        desktop_ready = False
        while time.monotonic() < deadline:
            boot._drain(1.0)
            clean = ANSI_RE.sub(b"", bytes(boot.pending))
            if GRAPHICAL_TARGET in clean and XORG_INPUT in clean and XTERM_STARTED in clean:
                desktop_ready = True
                break
            if b"kernel panic" in clean or b"emergency" in clean:
                print("[ERROR] kernel panic or emergency mode", flush=True)
                break

        if not desktop_ready:
            print("[WARN] desktop/xterm may not be fully ready, continuing anyway", flush=True)

        # ---- Settle ----
        time.sleep(SETTLE_SECONDS)
        screendump(MON_SOCK, SCREENSHOT_DIR / "01-before-backspace.ppm")
        print("[screenshot] 01-before-backspace.ppm", flush=True)

        # ---- Get monitor connection ----
        mon = mon_connect(MON_SOCK)
        if mon is None:
            print("[ERROR] cannot connect to QEMU monitor", flush=True)
            return 1

        # ---- Type test text into xterm ----
        # xterm should have focus by default.  Type "hello" character by character.
        print("[test] typing 'hello' into xterm", flush=True)
        for ch in "hello":
            sendkeys(mon, ch)
        time.sleep(1.0)
        screendump(MON_SOCK, SCREENSHOT_DIR / "02-hello-typed.ppm")
        print("[screenshot] 02-hello-typed.ppm", flush=True)

        # ---- Send backspace 5 times to erase "hello" ----
        print("[test] sending backspace 5 times", flush=True)
        for _ in range(5):
            sendkeys(mon, "backspace")
        time.sleep(1.0)
        screendump(MON_SOCK, SCREENSHOT_DIR / "03-backspace-erased.ppm")
        print("[screenshot] 03-backspace-erased.ppm", flush=True)

        # ---- Also test Ctrl+W (word erase) ----
        # Type "hello world" then Ctrl+W
        print("[test] typing 'hello world' then Ctrl+W", flush=True)
        for ch in "hello world":
            sendkeys(mon, ch)
        time.sleep(0.5)
        screendump(MON_SOCK, SCREENSHOT_DIR / "04-hello-world.ppm")
        print("[screenshot] 04-hello-world.ppm", flush=True)

        # Ctrl+W = send ctrl-w
        sendkeys(mon, "ctrl-w")
        time.sleep(1.0)
        screendump(MON_SOCK, SCREENSHOT_DIR / "05-ctrl-w-erased.ppm")
        print("[screenshot] 05-ctrl-w-erased.ppm", flush=True)

        # ---- Final screenshot ----
        time.sleep(1.0)
        screendump(MON_SOCK, SCREENSHOT_DIR / "06-final.ppm")
        print("[screenshot] 06-final.ppm", flush=True)

        mon.close()
        print("[test] DONE — screenshots in", SCREENSHOT_DIR, flush=True)

    except TimeoutError as e:
        print(f"[ERROR] timeout: {e}", flush=True)
        screendump(MON_SOCK, SCREENSHOT_DIR / "99-timeout.ppm")
        return 1
    finally:
        boot.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())