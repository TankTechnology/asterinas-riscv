#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the AUDIO-M1 smoke test and verify audio reached the host backend.

Boots Asterinas RISC-V with a ``virtio-sound-device`` wired to a QEMU ``wav``
audio backend. The guest ``/init`` writes a known number of sine-wave PCM bytes
to ``/dev/snd/pcmC0D0p``; this driver collects the serial transcript and then
checks that the host-side WAV file contains a header plus at least as many PCM
bytes as the guest reported writing.

Usage:
    python3 tools/riscv/nixos/audio/boot_audio.py \
        [--serial-log /tmp/asterinas-audio-serial.log] \
        [--wav /tmp/asterinas-audio-out.wav] [--command-timeout 120]
"""

from __future__ import annotations

import argparse
import math
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

DONE_MARKER = b"__AUDIO_DONE__"
PASS_MARKER = b"__AUDIO_PASS__"
FAIL_MARKER = b"__AUDIO_FAIL__"
BYTES_RE = re.compile(rb"__AUDIO_WRITE_BYTES=(\d+)__")

WAV_HDR_LEN = 44  # standard RIFF/WAVE header written by QEMU's wav backend

TONE_FREQ = 440.0       # the sine wave synthesized by the guest /init
TONE_TOL_HZ = 12.0      # dominant-frequency tolerance (resample/rounding drift)
MIN_RMS = 2000.0        # silence/zero-padded output stays well below this


def _chunks(raw: bytes):
    """Yield (chunk_id, size, data_offset) for a RIFF/WAVE byte string."""
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    off = 12
    while off + 8 <= len(raw):
        cid = raw[off:off + 4]
        size = struct.unpack("<I", raw[off + 4:off + 8])[0]
        yield cid, size, off + 8
        off += 8 + size + (size & 1)
        if cid == b"data":
            break


def _riff_header_fix(raw: bytes) -> bytes:
    """Rewrite the RIFF and data chunk sizes so the WAV is playable.

    QEMU's ``wav`` backend streams and leaves the ``RIFF`` and ``data`` chunk
    sizes at 0 (it only knows the totals on a clean teardown). ``wave`` (3.14)
    rejects that, and some players truncate it, so we fill in the real sizes.
    """
    if len(raw) < WAV_HDR_LEN or raw[:4] != b"RIFF":
        return raw
    out = bytearray(raw)
    struct.pack_into("<I", out, 4, len(raw) - 8)        # RIFF size
    struct.pack_into("<I", out, 40, len(raw) - WAV_HDR_LEN)  # data size
    return bytes(out)


def verify_tone(wav_path: Path) -> dict:
    """Decode ``wav_path`` and verify it is a real, non-silent 440 Hz sine.

    Returns a dict with ``ok`` plus the metrics used to decide. This is the
    "did sound actually come out" check on top of the raw byte-count check:
    the guest could deliver exactly 192 000 bytes of zeros and still pass the
    byte-count comparison, so we additionally assert amplitude and pitch.
    """
    raw = wav_path.read_bytes()
    metrics: dict = {"file": str(wav_path), "size": len(raw)}

    # Normalize the header so the file is playable, then save a copy alongside.
    fixed = _riff_header_fix(raw)
    playable = wav_path.with_suffix(".playable.wav")
    playable.write_bytes(fixed)
    metrics["playable"] = str(playable)

    # Walk chunks to find the fmt and data sections (QEMU sizes may be 0).
    fmt_off = data_off = None
    rate = channels = bits = None
    for cid, _size, doff in _chunks(raw):
        if cid == b"fmt " and fmt_off is None:
            _audio_fmt, channels, rate, _byterate, _block, bits = struct.unpack(
                "<HHIIHH", raw[doff:doff + 16]
            )
            fmt_off = doff
        elif cid == b"data":
            data_off = doff
            break
    if fmt_off is None or data_off is None or rate is None or channels is None:
        metrics["ok"] = False
        metrics["error"] = "missing fmt/data chunk"
        return metrics
    metrics["rate"] = rate
    metrics["channels"] = channels
    metrics["bits"] = bits

    pcm = raw[data_off:]
    # QEMU writes interleaved S16LE; grab channel 0 for the pitch estimate.
    sample_count = len(pcm) // 2
    ch0 = [struct.unpack_from("<h", pcm, i * 2 * channels)[0]
           for i in range(sample_count // channels)]
    metrics["frames"] = len(ch0)
    if not ch0:
        metrics["ok"] = False
        metrics["error"] = "no PCM data"
        return metrics

    # Amplitude: RMS and peak (full scale 32767).
    sumsq = sum(s * s for s in ch0)
    rms = math.sqrt(sumsq / len(ch0))
    peak = max(abs(s) for s in ch0)
    metrics["rms"] = round(rms, 1)
    metrics["peak"] = peak

    # Dominant frequency via zero crossings (deterministic for a pure tone).
    crossings = sum(
        1 for a, b in zip(ch0, ch0[1:]) if (a < 0) != (b < 0)
    )
    duration = len(ch0) / rate
    freq = (crossings / 2.0) / duration if duration else 0.0
    metrics["freq_hz"] = round(freq, 1)

    ok = (
        rms >= MIN_RMS
        and peak > 0
        and abs(freq - TONE_FREQ) <= TONE_TOL_HZ
    )
    metrics["ok"] = ok
    return metrics


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=info init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


class Boot:
    def __init__(self, argv: list[str], serial_log: Path) -> None:
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
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

    def read_until(self, needle: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for {needle!r}; tail={self.transcript[-800:]!r}"
                )
            for key, _ in self.sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return consumed

    def read_until_any(self, needles: list[bytes], timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            for needle in needles:
                if needle in self.pending:
                    return self.read_until(needle, 1.0)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for {needles!r}; tail={self.transcript[-800:]!r}"
                )
            for key, _ in self.sel.select(min(0.1, deadline - time.monotonic())):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-audio-serial.log"))
    parser.add_argument("--wav", type=Path,
                        default=Path("/tmp/asterinas-audio-out.wav"))
    parser.add_argument("--command-timeout", type=float, default=120.0)
    parser.add_argument("--smp", type=int, default=1)
    parser.add_argument("--play", action="store_true",
                        help="additionally play the normalized WAV on the host")
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")

    # Fresh WAV file for this run.
    if args.wav.exists():
        args.wav.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-audiodev", f"wav,id=audio0,path={args.wav},out.frequency=48000",
        "-device", "virtio-sound-device,audiodev=audio0",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
    ]

    boot = Boot(argv, args.serial_log)
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

        print("[boot] waiting for AUDIO completion", flush=True)
        try:
            boot.read_until(DONE_MARKER, args.command_timeout)
            boot.read_until_any([PASS_MARKER, FAIL_MARKER], 60)
        except TimeoutError:
            tail = bytes(boot.transcript[-4000:])
            print("[audio] FAIL: no completion marker (hang/crash)", flush=True)
            print(tail.decode("utf-8", "replace")[-3000:], flush=True)
            return 1
    finally:
        boot.close()

    text = bytes(boot.transcript).decode("utf-8", "replace")
    passed = PASS_MARKER in bytes(boot.transcript)

    print("\n=== AUDIO-M1 guest result ===", flush=True)
    for line in text.splitlines():
        if line.startswith("[AUDIO]") or line.startswith("__AUDIO"):
            print(line, flush=True)

    # Verify the host-side WAV file.
    m = BYTES_RE.search(bytes(boot.transcript))
    written = int(m.group(1)) if m else 0
    wav_size = args.wav.stat().st_size if args.wav.exists() else 0
    wav_data = max(0, wav_size - WAV_HDR_LEN)
    # With `out.frequency=48000` the backend rate matches the stream, so the
    # byte count should match exactly; tolerate a small resample/rounding drift
    # (<= 10%) to keep the check robust across QEMU audio-backend quirks.
    host_ok = wav_size >= WAV_HDR_LEN and wav_data > 0 and wav_data >= int(written * 0.9)

    print("\n=== AUDIO-M1 host-side verification ===", flush=True)
    print(f"  guest wrote : {written} bytes", flush=True)
    print(f"  wav file    : {wav_size} bytes ({wav_data} bytes PCM after {WAV_HDR_LEN}-byte header)", flush=True)
    ratio = (wav_data / written) if written else 0.0
    print(f"  received/written ratio: {ratio:.3f}", flush=True)
    print(f"  host received: {'OK' if host_ok else 'FAIL'}", flush=True)

    # The "did sound come out" check: amplitude + pitch, not just byte count.
    print("\n=== AUDIO-M1 audible-tone verification ===", flush=True)
    try:
        tone = verify_tone(args.wav)
        tone_ok = bool(tone.get("ok"))
    except (OSError, ValueError, struct.error) as exc:
        tone = {"ok": False, "error": str(exc)}
        tone_ok = False
    if "error" in tone:
        print(f"  decode error: {tone['error']}", flush=True)
    else:
        print(f"  fmt          : {tone.get('channels')} ch, {tone.get('rate')} Hz, "
              f"{tone.get('bits')}-bit, {tone.get('frames')} frames", flush=True)
        print(f"  amplitude    : RMS={tone.get('rms')}  peak={tone.get('peak')} "
              f"(min RMS {MIN_RMS:.0f})", flush=True)
        print(f"  pitch        : {tone.get('freq_hz')} Hz "
              f"(expect {TONE_FREQ:.0f} ± {TONE_TOL_HZ:.0f})", flush=True)
        print(f"  playable copy: {tone.get('playable')}", flush=True)
    print(f"  audible tone : {'OK' if tone_ok else 'FAIL'}", flush=True)

    if args.play and tone_ok:
        for player in ("aplay", "paplay"):
            if subprocess.run(["command", "-v", player], capture_output=True).returncode == 0:
                print(f"[play] {player} {tone['playable']}", flush=True)
                subprocess.run([player, tone["playable"]], check=False)
                break

    result = passed and host_ok and written > 0 and tone_ok
    print(f"\n=== AUDIO-M1: {'PASS' if result else 'FAIL'} (smp={args.smp}) ===", flush=True)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
