#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""DRM-M5 full-system integration boot.

Boots the merged DRM-tree kernel (DRM + ALSA + clock_getres) into the systemd
desktop (Xorg *modesetting* on ``/dev/dri/card0`` + matchbox-wm + NetSurf), and
runs the ALSA smoke test (``aplay`` through virtio-sound) in the same guest.
Verifies all three subsystems in a single boot:

1. **DRM / desktop** — systemd reaches ``graphical.target`` and Xorg starts on
   the modesetting driver; a QEMU ``screendump`` captures the rendered desktop.
2. **ALSA** — the guest ``alsa-test`` oneshot plays a 440 Hz tone; the host
   decodes QEMU's ``wav`` backend output and checks amplitude + pitch.
3. **NetSurf** — ``netsurf.service`` starts and renders the bundled local home
   page (a second screendump histogram shows the page painted).

Usage:
    python3 tools/riscv/nixos/m5/boot_m5.py \
        [--serial-log /tmp/drm-m5/serial.log] [--screenshot /tmp/drm-m5/shot.ppm]
"""

from __future__ import annotations

import argparse
import math
import os
import re
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

REPO = Path(__file__).resolve().parent.parent.parent.parent.parent
UBOOT = Path("/tmp/drm-m5/u-boot")
BOOT_DISK = Path("/tmp/drm-m5/boot.ext4")
MON_SOCK = Path("/tmp/drm-m5/mon.sock")

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
# The ~95 MB raw-cpio initramfs would clobber a DTB at 0x8800_0000; relocate the
# DTB above the load ceiling (2 GiB RAM => 0x9000_0000 is clear), matching the
# systemd-desktop runner.
DTB_LOAD = 0x9000_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
GRAPHICAL_TARGET = b"Reached target Graphical Interface"
XORG_BANNER = b"X.Org X Server"
XORG_STARTED = b"Started Xorg display server"
XORG_INPUT = b"Adding extended input device"
NETSURF_STARTED = b"Started NetSurf web browser"
EMERGENCY = b"Welcome to emergency mode"

ALSA_DONE = b"__ALSA_DONE__"
ALSA_PASS = b"__ALSA_PASS__"
ALSA_FAIL = b"__ALSA_FAIL__"

PANIC_MARKERS = [
    b"kernel panic", b"Kernel panic", b"page fault handler failed",
    b"Oops", b"BUG:", b"panic!",
]


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


class Boot:
    def __init__(self, argv: list[str], serial_log: Path) -> None:
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
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
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)

    def read_until(self, needle: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for {needle!r}; tail={self.transcript[-800:]!r}")
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
        self.log_file.close()


def screendump(sock: Path, path: Path) -> None:
    if not sock.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock))
            s.settimeout(5)
            s.sendall(f"screendump {path}\n".encode())
            time.sleep(2.0)
    except OSError:
        pass


def riff_header_fix(src: Path, dst: Path) -> None:
    """QEMU's wav backend leaves RIFF/data chunk sizes at 0; rewrite them."""
    data = bytearray(src.read_bytes())
    if len(data) < 44:
        raise ValueError("wav too short")
    data_size = len(data) - 44
    data[4:8] = struct.pack("<I", 36 + data_size)
    data[40:44] = struct.pack("<I", data_size)
    dst.write_bytes(data)


def verify_tone(wav_path: Path) -> dict:
    """Decode the host WAV and assert amplitude + pitch (440 Hz)."""
    if not wav_path.exists() or wav_path.stat().st_size == 0:
        return {"ok": False, "error": "missing/empty wav"}
    playable = wav_path.with_suffix(".playable.wav")
    try:
        riff_header_fix(wav_path, playable)
        with wave.open(str(playable), "rb") as w:
            ch = w.getnchannels()
            rate = w.getframerate()
            bits = w.getsampwidth() * 8
            frames = w.getnframes()
            raw = w.readframes(frames)
    except (OSError, ValueError, wave.Error) as exc:
        return {"ok": False, "error": f"decode: {exc}"}

    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw[: n * 2])
    # Downmix to mono (average channels) for amplitude/pitch.
    mono = samples[::ch] if ch > 1 else samples
    nmono = len(mono)
    rms = math.sqrt(sum(s * s for s in mono) / nmono) if nmono else 0.0
    peak = max(abs(s) for s in mono) if nmono else 0

    # Pitch via zero crossings (deterministic for a pure tone).
    crossings = 0
    for i in range(1, nmono):
        if mono[i - 1] < 0 <= mono[i] or mono[i - 1] >= 0 > mono[i]:
            crossings += 1
    freq = crossings / 2.0 * rate / nmono if nmono else 0.0

    ok = rms >= 2000 and abs(freq - 440.0) <= 12.0
    return {
        "ok": ok, "channels": ch, "rate": rate, "bits": bits, "frames": frames,
        "rms": round(rms, 1), "peak": peak, "freq_hz": round(freq, 1),
        "playable": str(playable),
    }


def histogram(path: Path) -> dict:
    """Rough colour histogram of a PPM screendump (for 'did the page paint')."""
    if not path.exists():
        return {}
    data = path.read_bytes()
    # PPM P6: header ends at the first whitespace-terminated maxval, then raw RGB.
    try:
        parts = data.split(b"\n", 3)
        if parts[0].strip() != b"P6":
            return {}
        dims = parts[1].split()
        header_len = len(parts[0]) + len(parts[1]) + len(parts[2]) + 3
        pix = data[header_len:]
        step = len(pix) // 4096
        counts = {}
        total = 0
        nonblack = 0
        cream = 0  # NetSurf home page body #f4e8d0
        blue = 0   # NetSurf home page headings #1a4f8b
        for i in range(0, len(pix) - 2, max(step, 1)):
            r, g, b = pix[i], pix[i + 1], pix[i + 2]
            total += 1
            if r > 24 or g > 24 or b > 24:
                nonblack += 1
            if r > 200 and g > 190 and 170 < b < 240:
                cream += 1
            if b > 100 and b > r + 40 and b > g + 40:
                blue += 1
            key = (r // 32 * 32, g // 32 * 32, b // 32 * 32)
            counts[key] = counts.get(key, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        return {
            "w": int(dims[0]), "h": int(dims[1]), "top": top,
            "total": total, "nonblack": nonblack, "cream": cream, "blue": blue,
        }
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/drm-m5/serial.log"))
    parser.add_argument("--screenshot", type=Path, default=Path("/tmp/drm-m5/shot.ppm"))
    parser.add_argument("--wav", type=Path, default=Path("/tmp/drm-m5/alsa-out.wav"))
    parser.add_argument("--init-timeout", type=float, default=300.0)
    parser.add_argument("--collect-timeout", type=float, default=300.0)
    parser.add_argument("--settle-seconds", type=float, default=120.0)
    parser.add_argument("--net", action="store_true")
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")
    if MON_SOCK.exists():
        MON_SOCK.unlink()
    if args.wav.exists():
        args.wav.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-device",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-audiodev", f"wav,id=audio0,path={args.wav},out.frequency=48000",
        "-device", "virtio-sound-device,audiodev=audio0",
        "-serial", "stdio",
        "-monitor", f"unix:{MON_SOCK},server,nowait",
    ]
    if args.net:
        argv.extend([
            "-netdev", "user,id=net0",
            "-device", "virtio-net-device,netdev=net0",
        ])

    boot = Boot(argv, args.serial_log)
    reached = "timeout"
    try:
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

        print("[boot] waiting for /init launcher", flush=True)
        boot.read_until(INIT_MARKER, args.init_timeout)
        print("[ok] /init reached (exec'ing systemd)", flush=True)

        print(f"[boot] collecting (timeout={args.collect_timeout}s)", flush=True)
        deadline = time.monotonic() + args.collect_timeout
        while time.monotonic() < deadline:
            clean = ANSI_RE.sub(b"", bytes(boot.pending))
            # ALSA runs as a oneshot Before=graphical.target, so by the time the
            # desktop is up the tone has already been played; the host verifies it
            # from the WAV backend (independent of the guest marker reaching us).
            if GRAPHICAL_TARGET in clean and XORG_STARTED in clean:
                reached = "all-up"
                break
            if EMERGENCY in clean:
                reached = "emergency"
                break
            if any(m in clean for m in PANIC_MARKERS):
                reached = "panic"
                break
            try:
                boot._drain(1.0)
            except RuntimeError:
                reached = "serial-closed"
                break
    except TimeoutError as e:
        reached = "timeout"
        print(f"[boot] {e}", flush=True)
    except RuntimeError as e:
        reached = "serial-closed"
        print(f"[boot] serial closed early: {e}", flush=True)
    finally:
        if args.settle_seconds > 0:
            print(f"[boot] settling {args.settle_seconds}s", flush=True)
            settle_deadline = time.monotonic() + args.settle_seconds
            while time.monotonic() < settle_deadline:
                try:
                    boot._drain(1.0)
                except RuntimeError:
                    break
        screendump(MON_SOCK, args.screenshot)
        boot.close()

    clean = ANSI_RE.sub(b"", bytes(boot.transcript))
    transcript = clean.decode("utf-8", "replace")

    markers = {
        "init-launcher": INIT_MARKER in clean,
        "graphical-target": GRAPHICAL_TARGET in clean,
        "xorg-started": XORG_STARTED in clean,
        "xorg-banner": XORG_BANNER in clean,
        "xorg-input-devices": XORG_INPUT in clean,
        "netsurf-started": NETSURF_STARTED in clean,
        "alsa-done": ALSA_DONE in clean,
        "alsa-pass": ALSA_PASS in clean,
        "alsa-fail": ALSA_FAIL in clean,
        "emergency": EMERGENCY in clean,
    }
    panics = [m.decode() for m in PANIC_MARKERS if m in clean]

    print("\n=== DRM-M5 guest result ===", flush=True)
    for k, v in markers.items():
        print(f"  {k}: {'OK' if v else 'MISSING'}", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)

    # Host-side: ALSA tone + screenshot histogram.
    print("\n=== ALSA audible-tone verification ===", flush=True)
    tone = verify_tone(args.wav)
    if "error" in tone:
        print(f"  decode error: {tone['error']}", flush=True)
        tone_ok = False
    else:
        print(f"  fmt       : {tone.get('channels')} ch, {tone.get('rate')} Hz, "
              f"{tone.get('bits')}-bit, {tone.get('frames')} frames", flush=True)
        print(f"  amplitude : RMS={tone.get('rms')}  peak={tone.get('peak')} "
              f"(min RMS 2000)", flush=True)
        print(f"  pitch     : {tone.get('freq_hz')} Hz (expect 440 +/- 12)", flush=True)
        print(f"  playable  : {tone.get('playable')}", flush=True)
        tone_ok = bool(tone.get("ok"))
    print(f"  audible   : {'OK' if tone_ok else 'FAIL'}", flush=True)

    print("\n=== screenshot histogram ===", flush=True)
    hist = histogram(args.screenshot)
    if hist:
        total = hist.get("total", 1) or 1
        nb_pct = 100.0 * hist.get("nonblack", 0) / total
        print(f"  size: {hist.get('w')}x{hist.get('h')}", flush=True)
        for rgb, cnt in hist.get("top", []):
            print(f"  colour {rgb}: {cnt}", flush=True)
        print(f"  non-black: {nb_pct:.1f}%  cream(#f4e8d0): {hist.get('cream', 0)} "
              f" blue(#1a4f8b): {hist.get('blue', 0)}", flush=True)
    else:
        print("  (no screenshot / unparsable)", flush=True)

    # NetSurf renders the local home page whose body is cream #f4e8d0 with blue
    # #1a4f8b headings. A solid-black screenshot means the page never painted.
    netsurf_rendered = bool(hist) and hist.get("cream", 0) > 0 and hist.get("blue", 0) > 0

    print("\n=== serial tail ===", flush=True)
    print(transcript[-4000:], flush=True)

    ok = (markers["graphical-target"] and markers["xorg-started"]
          and markers["xorg-banner"] and tone_ok and netsurf_rendered)
    print(f"\n=== DRM-M5: {'PASS' if ok else 'FAIL'} (smp={args.smp}) ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
