# Megrez desktop and Firefox performance plan

**Goal:** On current `main`, make the board's Debian desktop and Firefox
repeatably usable, then improve the measured dominant bottleneck without
losing serial recovery. The user-facing target is at least a twofold
improvement in the affected interaction or playback metric, but this is an
acceptance target, not a result already obtained.

The existing [daily-use objective](../../porting/2026-09-25-firefox-desktop-daily-use-objective.md)
defines the desktop behavior. Keep DRM device enumeration, GPU rendering, and
physical HDMI output as separate claims; neither a DRM node nor an X11
screenshot proves GPU acceleration or monitor output.

## 1. Restore a reproducible boot

- [x] Record the exact kernel, Stage1, DTB, root, selector, and serial identities
  before changing the board.
- [x] Reproduce the U-Boot bootargs overflow and reject overlong desktop menu
  entries in the publisher, with a boundary regression test.
- [x] Identify and back up the stale partition-2 DRM-only service override that
  conflicts with this root's fbdev provider; remove only that override.
- [x] Make persistent HOME an explicit desktop boot-plan setting with a distinct
  immutable generation and document its effect.
- [ ] Confirm a bounded desktop boot and fresh nonce-framed root access, close
  and reopen the serial connection, then confirm access again. Verify a
  separate software reboot returns to RockOS and leaves partition 2 unmounted.

If the watchdog cannot recover a hard hang, stop board changes and record the
last observed phase. Hardware reset requires an operator; a successful X11
process list is not a display or recovery pass.

## 2. Establish a short physical baseline

- [ ] Run the existing seven-group Firefox daily-use gate with a persistent
  home and the same signed root. Record startup, pointer/scroll, context menu,
  and a fixed 10-second 720p video sample. Keep raw logs and artifact hashes.
- [ ] Capture the framebuffer or X11 image and check wallpaper, panel, browser,
  and resolution automatically. Mark physical HDMI appearance unverified in
  the absence of an external capture device or operator observation.
- [ ] Attribute the slowest reproducible case with per-thread CPU, runnable
  wait, kernel trace, and bounded Firefox PC samples. Measure probe overhead.

## 3. Change one proven bottleneck and qualify it

- [ ] Choose a kernel or user-space change only after the baseline identifies
  its cost. The existing 720p evidence points primarily to Firefox SWGL YUV
  conversion; do not assume a DRM node will accelerate it.
- [ ] Add a focused regression test, implement the change on `main`, and run
  the shortest relevant QEMU and board correctness gates.
- [ ] Compare baseline/candidate/baseline on identical artifacts except the
  change, with bounded runs and instrumentation off. Report median, spread,
  dropped frames, and regressions. Keep the twofold target open unless the
  observed data meets it.

## 4. Graphics qualification

- [ ] Validate the GPU render path with a hardware-produced pixel and fence
  completion; validate physical HDMI separately. Do not enable Firefox GPU
  rendering until both the user-space driver protocol and display path pass.
