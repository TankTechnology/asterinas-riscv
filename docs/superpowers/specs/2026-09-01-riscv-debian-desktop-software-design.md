# RISC-V Debian Desktop Software Validation Design

**Goal:** Add a reproducible Debian RISC-V desktop profile containing the
official `netsurf-gtk`, `vim`, and `ffmpeg` packages, then validate it in QEMU
before using the same artifact for bounded Megrez desktop testing.

## Scope

The existing `desktop-m5-network` image remains immutable. A new profile adds
user-facing software and a small evidence service. The profile is built from
Debian Trixie signed metadata through the existing rootfs builder; no live
modification of the board root disk is required for the primary acceptance
path. NetSurf is the Debian `riscv64` package, not the legacy GTK2 source build.

QEMU is the first gate and uses four harts. It checks package identity,
desktop startup, NetSurf rendering/navigation, `vim` editing, and `ffmpeg` /
`ffprobe` processing of a deterministic 16x16 media fixture generated inside
the guest. The physical gate
reuses the same image and checks GMAC/DNS/TLS, desktop, NetSurf, and software
launches with finite timeouts.

## Network and installation model

The build uses the existing TUNA mirror and signed Debian archive metadata.
Host proxy use is explicit and temporary. For a board with unreliable apt
access, the complete package closure is staged and hash-verified in the image;
the physical test does not depend on an unbounded online `apt` session.

## Non-goals

This change does not implement full modern-browser JavaScript support, USB
Ethernet/Wi-Fi, native DRM acceleration, or a new Firefox milestone. Firefox
remains a later browser target after the NetSurf and GMAC gates pass.

The M9 profile deliberately reuses manifest schema 5. The schema identifies
the signed Debian metadata shape; the profile name and filesystem label/UUID
identify the new software payload. This keeps existing contract readers
backwards-compatible while preventing the M9 image from being mistaken for
the immutable M5 root.

## Acceptance

The QEMU gate must emit one machine-readable pass record containing the
profile, package versions, desktop markers, browser markers, and software
smoke results. A physical run publishes the same evidence plus board and
network diagnostics. Any failure is classified without retrying or resetting
the board indefinitely.
