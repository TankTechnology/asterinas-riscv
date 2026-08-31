# Firefox RISC-V Runtime Acceptance Design

Date: 2026-08-31

## Goal

Produce a separately identifiable Debian `browser-m5` rootfs and a bounded QEMU
startup/content acceptance path for Firefox ESR on Asterinas RISC-V. The first
runtime milestone proves that Firefox can start as a non-root X11 client and
answer a loopback Marionette probe; it does not claim modern-web compatibility
or Megrez board support.

## Current context

The current branch already contains the `browser-m5` profile, schema-6 signed
base/security-source validation, Firefox launcher, sandbox checks, Marionette
content client, and QEMU classifier. The installed Megrez browser-quality image
is still the NetSurf-based `desktop-m5-network` profile. This work must keep
that image immutable and create a separate Firefox artifact before any board
run.

## Architecture

The rootfs builder remains the sole producer of the Firefox image. It admits
only the explicit Firefox profile package set, authenticates both Debian
Trixie and Trixie-security metadata, verifies every package/index relationship,
and publishes the existing fixed-size image plus manifest and lock files.

Runtime evidence is layered:

1. A host-side static preflight verifies the manifest, package identities,
   RISC-V ELF loader/dependency shape, CA bundle, fonts, and image capacity.
2. A short QEMU cold-start probe samples process, Xorg, Marionette, and serial
   state at bounded checkpoints. It terminates with a classified failure rather
   than waiting through the full content gate when Firefox never becomes ready.
3. Only after readiness does the existing offline Marionette gate validate the
   repository-owned JavaScript and VP8/WebM fixture with Firefox in a private
   loopback namespace.

The existing online network gate and physical Megrez gate remain later stages;
they consume the same immutable Firefox artifact only after the offline QEMU
stages pass.

## Acceptance boundaries

- Rootfs: `browser-m5` manifest schema 6, signed source roles `base` and
  `security`, all required Firefox files are riscv64 or architecture-neutral,
  and the 1 GiB image is complete and hashable.
- QEMU startup: SMP=4, Sv39, existing Xorg fbdev/evdev stack, Firefox process
  visible as the service main process, Marionette on loopback, no
  `--no-sandbox` or explicit sandbox-disabling environment, and a bounded
  `READY` marker.
- QEMU content: exact ordered JavaScript, media `canplay`, media `ended`, and
  private-loopback markers from the live DOM; no external content dependency.
- Not included in this milestone: physical HDMI capture, DRM/GPU acceleration,
  audio, encrypted media, Chromium, or a claim that arbitrary modern sites
  render correctly.

## Failure handling

The probe must distinguish artifact/preflight failure, Firefox process exit,
missing Xorg/input, missing Marionette, and cold-start timeout. It must retain
the serial transcript and process diagnostics, clean up QEMU, and leave the
existing board image untouched. A timeout is evidence of an incomplete gate,
not a pass and not a request for a manual board reset.

## Verification

Static and classifier tests run before any QEMU process. The QEMU startup probe
is exercised with a fake transcript/process fixture for timeout and readiness
classification, then with the real `browser-m5` image when the container has
debootstrap, qemu-riscv64-static, ffmpeg, and binfmt support. The full offline
Marionette gate is not attempted until the short probe reports readiness.
