# QEMU desktop M4 render-path diagnosis (2026-10-08)

This record is a diagnostic checkpoint. It is not a desktop pass and does not
authorize a physical-board boot.

## What was reproduced

- The current WriteCombining kernel registers a 1280x1024, 32-bit
  `BgrReserved` framebuffer. A guest user process can mmap the complete
  framebuffer and fill it with a fixed color while the mapping remains live;
  QEMU `screendump` then reports 1,310,720 non-background pixels. The QEMU
  bochs scanout and the `/dev/fb0` user mapping therefore work.
- Switching the RISC-V framebuffer mapping temporarily to Uncacheable did not
  change the black Xorg capture. Cache policy is not the current explanation.
- The desktop service passed device, Xorg, and process checks, but Xorg client
  windows did not produce a useful scanout. The strict framebuffer probe
  reported `framebuffer contains a single color or insufficient content`.
- A controlled root-console check after the desktop shell marker mapped
  `/dev/fb0` for its exact 1280x1024x4-byte length and filled the whole mapping
  with a fixed BgrReserved color. The write returned status `0`; QEMU's
  screendump then contained 1,310,719 non-background pixels and one uniform
  color. This is an intentional positive scanout check (the normal diversity
  gate rejects a uniform frame), and proves that the current QEMU BAR, guest
  fbdev mmap, and host scanout can carry a color frame.
- A retained debug-console transcript from an older image also shows the
  device-access helper leaving a child in the runnable state before Xorg was
  launched, after `/dev/fb0` and the two input nodes appeared. This is
  consistent with the known VFS mode-change stall, but does not by itself
  prove which syscall was stuck. Ordered QEMU permission changes are now
  wrapped in a five-second `timeout`; a repeat will fail closed with a guest
  marker instead of waiting indefinitely.

## Configuration defects fixed in the source tree

- M4 no longer installs the legacy `/etc/X11/xorg.conf.d/20-asterinas.conf`
  symlink. Xorg's explicit QEMU configuration was otherwise mixed with that
  legacy file and attempted `/run/asterinas-input/*` paths that do not exist in
  ordered QEMU mode.
- M4 selects the no-input Xorg configuration before the ordered-QEMU branch,
  so an explicit `ASTERINAS_DESKTOP_INPUT_REQUIRED=0` cannot be overridden by
  the command-line identity token.
- Standalone `desktop-m4` no longer starts the browser-free core evidence unit
  in parallel with its normal evidence unit. The duplicate probes caused
  concurrent Python/ext2 reads and intermittent `source code string cannot
  contain null bytes` failures. The core unit remains available to profiles
  that explicitly need it and is guarded by
  `ConditionEnvironment=ASTERINAS_DESKTOP_BROWSER_ENABLED=0`.

## Current gate status

The strict QEMU desktop gate remains failed because the Xorg-produced frame has
insufficient pixel diversity. No physical board was started in this cycle. A
physical attempt is still blocked until a clean QEMU frame and the two-boot
recovery check both pass.

## Follow-up isolation (same kernel and root image)

- A diagnostic run waited for the client marker and captured the X window tree.
  The panel was `1280x34`, while the PCManFM and NetSurf windows remained
  `10x10`; the terminal client had a `604x394` child window. The framebuffer
  sample before any diagnostic write was a mostly black frame with only 43--55
  sampled colors, depending on startup timing, and never reached the desktop
  diversity threshold.
- Starting Xorg with `ShadowFB=true` did not change the application frame. A
  temporary `-retro` run produced a deterministic gray Xorg pattern (55
  colors and 327,744 non-background pixels), proving that the Xorg fbdev server
  itself can render to the scanout. The application windows then failed to
  replace that root pattern, so this is now an X client/WM paint or mapping
  issue rather than a QEMU color limitation.
- A guest mapping that writes a full frame without `mmap.flush()` did not alter
  the QEMU screenshot; the same mapping became visible after an explicit flush.
  A separate periodic-flush process did not make the Xorg frame appear. A
  temporary RISC-V `Uncacheable` framebuffer mapping also reproduced the same
  application failure. The experiment is not retained as a production change:
  the QEMU fbdev read path must remain on the existing `WriteCombining` policy
  until a non-blocking read protocol is available.
- The diagnostic root console also observed `user@1000.service` failing while
  the desktop session and Xorg continued. This is recorded as a separate
  userspace readiness defect; it is not evidence of a kernel or QEMU scanout
  failure.

## Artifact provenance check

- The earlier desktop gate records used
  `target/qemu-uboot/cache/u-boot-build/u-boot` (SHA-256
  `b34728358ee292ae57d066578a71503b7ccb634827a4df7538c558a5f3cc9efd`).
  The separate `target/qemu-uboot/u-boot` and `target/qemu-probe/u-boot`
  artifacts currently have SHA-256
  `738775f52dcfad98dfae4b851a6dc3118db30bbe6455c7df3573a84133db8a5d`.
  A bounded diagnostic using the latter did not reach the desktop marker;
  this artifact must not be substituted silently in the desktop gate.
- Repeating the diagnostic with the cached desktop U-Boot reached the same
  Xorg/client failure recorded above. This preserves the conclusion that the
  QEMU color path is functional while the application-paint gate is still
  unresolved; it does not authorize a physical boot.

The stricter gate was then split into a guest readback check and a host
scanout check. With the threshold reduced to two changed sampled pixels, the
WriteCombining run produced:

```
DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 stride=5120 bpp=32 format=BgrReserved
DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=ad7facb2586fc6e966c004d7d1d16b024f5805ff7cb47c7a85dabd8b48892ca7
DEBIAN_DESKTOP_M4_FB_COLORS distinct=3 non_background=2
```

The following QEMU HMP `screendump` still contained one color, so the gate
failed closed with `PPM framebuffer contains a single color`. A separate
Uncacheable/PBMT_IO kernel produced the same result (`target/qemu-desktop-m4-pbmt-io-gate/`), so changing the page memory type is not sufficient. The
final source restores the existing WriteCombining policy; the last successful
kernel build is `d6554801dcb9e902ee773fa3b2c781d149aa969ca4d8aead1e439d350e1f5832`.

## 2026-10-08 volatile-read experiment

The RISC-V `IoMem::read_fallible` path was temporarily changed to read each
aligned chunk with a volatile MMIO load. The experiment was reverted after a
fresh QEMU run: the guest probe stopped producing output and hit its 30-second
bounded timeout. A bochs framebuffer BAR is not a safe substitute for a
register-style volatile device read. The repository therefore retains the
original bounded `__memcpy_fallible` path and `WriteCombining` mapping; future
readback work must first define a non-blocking scanout protocol.

## QEMU recovery gate (2026-10-08)

The independent recovery gate completed one desktop epoch followed by the
guest's bounded software reboot timer. Its result is
`target/qemu-desktop-m4-recovery-20261008/result.json` with `passed: true`.
The serial evidence contains the ordered desktop marker followed by a fresh
`OpenSBI v1.7` banner and `U-Boot 2026.07` prompt. The input hashes include
the WriteCombining kernel (`d6554801...`), cached desktop U-Boot
(`b3472835...`), QEMU DTB, stage-1 initramfs, and the frozen QEMU root image.

This gate intentionally disables the pixel probe through a boot-scoped
environment override. Pixel readback and host HMP scanout remain a separate
gate because a transient ext2/Python source-read failure previously produced
`source code string cannot contain null bytes`; that failure must not be
confused with loss of the desktop reboot/recovery path. The strict desktop
scanout gate is still failed closed on the host screenshot's single-color
result, so this recovery pass does not authorize a physical boot.
