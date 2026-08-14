#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the NIXOS-STAGE2-M1 initramfs and report systemd + nix progress.

Reuses the SYSTEMD-DESKTOP-M1 QEMU/U-Boot driver (Boot class, booti handoff,
bochs framebuffer) and adds the nix milestones: the activation service must run
(``__NIX_ACTIVATION_OK__``) and the smoke service must run nix-profile-installed
binaries by bare name (``hello``, ``nixos-info``, ``jq --version``,
``curl --version``), emitting ``___NIX_RUN_*___`` markers.

Success = systemd reaches graphical.target AND Xorg brings up input devices AND
the nix activation + smoke markers are all present.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import boot_systemd_desktop as desktop

ANSI_RE = desktop.ANSI_RE

# --- nix milestones -------------------------------------------------------
NIX_ACTIVATION = b"__NIX_ACTIVATION_OK__"
NIX_SMOKE_BEGIN = b"___NIX_SMOKE_BEGIN___"
NIX_SMOKE_END = b"___NIX_SMOKE_END___"

# Each nix binary the smoke runs, keyed by the marker line that precedes it and
# the substring that proves it actually executed (not just was invoked).
NIX_RUNS: dict[str, tuple[bytes, bytes]] = {
    "hello": (b"___NIX_RUN_hello___", b"Hello, world!"),
    "nixos-info": (b"___NIX_RUN_nixos-info___", b"nix      :"),
    "jq": (b"___NIX_RUN_jq___", b"jq-1.8.2"),
    "curl": (b"___NIX_RUN_curl___", b"curl 8.21.0"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-sd-nixos.log"))
    parser.add_argument("--collect-timeout", type=float, default=300.0)
    parser.add_argument("--screenshot", type=Path,
                        default=Path("/tmp/asterinas-sd-nixos.ppm"))
    args = parser.parse_args()

    if not desktop.UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {desktop.UBOOT}")
    if not desktop.BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {desktop.BOOT_DISK}")
    if desktop.MON_SOCK.exists():
        desktop.MON_SOCK.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-display", "none",
        "-no-reboot",
        "-kernel", str(desktop.UBOOT),
        "-drive", f"if=none,format=raw,file={desktop.BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "bochs-display",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-serial", "stdio",
        "-monitor", f"unix:{desktop.MON_SOCK},server,nowait",
    ]

    boot = desktop.Boot(argv, args.serial_log)
    reached = "timeout"
    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)

        for name, text, expected in desktop.uboot_commands():
            print(f"[uboot] {name}", flush=True)
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print("[boot] waiting for /init launcher", flush=True)
        boot.read_until(desktop.INIT_MARKER, 120)
        print("[ok] /init reached (exec'ing systemd)", flush=True)

        print(f"[boot] collecting systemd+nix output (timeout={args.collect_timeout}s)",
              flush=True)
        deadline = time.monotonic() + args.collect_timeout
        while time.monotonic() < deadline:
            clean_pending = ANSI_RE.sub(b"", bytes(boot.pending))
            if desktop.GRAPHICAL_TARGET in clean_pending and desktop.XORG_INPUT in clean_pending:
                reached = "desktop-up"
                break
            if desktop.EMERGENCY in clean_pending:
                reached = "emergency"
                break
            if any(m in clean_pending for m in desktop.PANIC_MARKERS):
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
    finally:
        desktop.screendump(desktop.MON_SOCK, args.screenshot)
        boot.close()

    raw = bytes(boot.transcript)
    clean = ANSI_RE.sub(b"", raw)
    transcript = clean.decode("utf-8", "replace")

    markers = {
        "init-launcher": desktop.INIT_MARKER in clean,
        "systemd-banner": desktop.VERSION_BANNER in clean,
        "basic-target": desktop.BASIC_TARGET in clean,
        "multi-user-target": desktop.MULTIUSER_TARGET in clean,
        "graphical-target": desktop.GRAPHICAL_TARGET in clean,
        "xorg-started": desktop.XORG_STARTED in clean,
        "xorg-input-devices": desktop.XORG_INPUT in clean,
        "matchbox-started": desktop.WM_STARTED in clean,
        "xpanel-started": desktop.PANEL_STARTED in clean,
        "pcmanfm-started": desktop.PCMANFM_STARTED in clean,
        "xterm-started": desktop.XTERM_STARTED in clean,
        "nix-activation": NIX_ACTIVATION in clean,
    }
    for name, (marker, proof) in NIX_RUNS.items():
        # The proof substring must appear *after* the marker line.
        m_idx = clean.find(marker)
        markers[f"nix-{name}"] = m_idx >= 0 and proof in clean[m_idx:]

    panics = [m.decode() for m in desktop.PANIC_MARKERS if m in clean]
    unimplemented = transcript.count("Unimplemented syscall")

    print("\n=== NIXOS-STAGE2-M1 result ===", flush=True)
    for k, v in markers.items():
        print(f"  {k}: {'OK' if v else 'MISSING'}", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    print(f"  unimplemented-syscall lines: {unimplemented}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)
    if args.screenshot.exists():
        print(f"  screenshot: {args.screenshot}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-6000:], flush=True)

    # Success: systemd desktop up AND the nix activation ran AND every nix
    # binary the smoke exercised actually executed.
    desktop_ok = markers["graphical-target"] and markers["xorg-started"]
    nix_ok = markers["nix-activation"] and all(
        markers[f"nix-{name}"] for name in NIX_RUNS
    )
    return 0 if (desktop_ok and nix_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
