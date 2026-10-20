# QEMU DRM Task 2/5 bounded status — 2026-10-16

This is a host/QEMU-only checkpoint. It does not include serial writes,
physical-board MMIO, board reboot, or a claim about EIC7700 PowerVR.

## Current stage

Task 2 (render-node/GEM) is now **complete for QEMU**. The current receipt
proves the card/render permission split, the single-file GEM
create/map/name/open/close/reject sequence, two independent `card0` files,
PRIME export/import with stale-fd rejection, and the bounded ENOMEM boundary.
The kernel-side cleanup implementation is covered by ktests. The immutable
runtime receipt is `2026-10-16-qemu-drm-gem-prime-enomem.md`.

Task 5 (Mesa/Xorg) has a fresh QEMU virgl happy-path receipt and the prior
recovery receipt. The new runtime serial contract explicitly skips `/dev/fb0`,
pins Xorg to `modesetting + kmsdev=/dev/dri/card0`, and rejects software
renderer strings.

## This round's closed gates

1. **DRM renderer classifier:** `classify_desktop_m4_drm()` rejects an EGL
   `PIXEL_PROBE` line containing `llvmpipe`, `softpipe`, `swrast`, or `swgl`.
2. **DRM rootfs contract:** the evidence script has a separate DRM readiness
   branch; it does not invoke the framebuffer probe for `DISPLAY_PROVIDER=drm`.
   It requires a regular `20-asterinas.conf` with
   `Driver "modesetting"` and `Option "kmsdev" "/dev/dri/card0"`.
3. **GEM host-resource lifetime:** final `RESOURCE_UNREF` is now issued only
   when the device-wide GEM reference count reaches zero. The same rule is
   used by `DriHandle::drop`, GEM close, RMFB and dma-buf close. Failed PRIME
   pool setup no longer increments a reference before the operation can fail.
4. **Build and unit checks:** the focused host gates, RISC-V kernel build, and
   filtered `object_lifetime` QEMU ktest all returned exit status 0.
5. **Fresh Task 5 runtime:** the new GL QEMU run passed with the updated overlay;
   it emitted the fbdev-skip, modesetting/kmsdev, sysfs, libdrm, virgl EGL,
   X11 screenshot, and desktop-ready markers.
6. **Task 2 GEM/PRIME/ENOMEM runtime:** the current kernel emitted all 11
   ordered markers; see `2026-10-16-qemu-drm-gem-prime-enomem.md`.
7. **GTK host scanout:** the same guest DRM scanout probe passed through
   `-display gtk` under Xvfb, with a 640x480 QMP screenshot and pixel hash;
   see `2026-10-16-qemu-drm-scanout-gtk.md`.

## Evidence and commands

| Gate | Result | Evidence |
| --- | --- | --- |
| DRM desktop classifier and recovery transcript tests | PASS, 24 tests | `tools/riscv/tests/test_debian_rootfs.py`; `tools/riscv/tests/test_debian_desktop_m4_drm_recovery_gate.py` |
| GEM/render-node host gates | PASS, 14 + 14 tests | `make test_riscv_drm_gem_unit test_riscv_drm_render_node_unit` |
| RISC-V kernel compile | PASS | `make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode` in the persistent dev container |
| GEM lifetime ktest | PASS, exit 0 | `cargo osdk test object_lifetime --target-arch riscv64 --features riscv_sv39_mode --scheme riscv --grub-boot-protocol=multiboot2` from `kernel/` |
| Existing virgl desktop runtime | PASS (prior receipt) | `2026-10-15-qemu-drm-desktop-current-v2.md` |
| Existing virgl recovery runtime | PASS (prior receipt) | `2026-10-15-qemu-drm-desktop-recovery.md` |
| Fresh DRM desktop runtime | PASS | `target/desktop-m4-current-overlay-20261016-status/gate-gl-r2/result.json` (`03a405874ca3ae66494da4fa9dfff1e21020d646a14179217d7098f5e0d88f10`); `desktop-m4-drm.serial.log` (`3f74921006284bb35d809be50ae3802c675eaca5eac22649eed2eaa09524be08`) |
| GTK DRM scanout runtime | PASS | `2026-10-16-qemu-drm-scanout-gtk.md`; QMP PPM pixel hash `2f90fdc98b17ea03e030efc6fed9a5028f2fab66c05d3577ee75161ce5c2f33a` |

The filtered ktest command emits no durable serial transcript in the OSDK
wrapper; its evidence here is the completed QEMU test process and exit status.
It is therefore not being used as a replacement for the runtime GEM receipt;
the PRIME and ENOMEM checks now have their own QEMU evidence.

## Next gate

The first attempt with the new overlay used the default 180-second boot
deadline and failed closed at the orchestration protocol boundary before the
desktop markers. Its retained `result.json`/serial log is not a pass. The
bounded rerun used a 300-second boot deadline and passed; the extra time is
recorded in the gate invocation rather than hidden as a retry. The failed
receipt is retained at
`target/desktop-m4-current-overlay-20261016-status/gate-gl/` (only its
generated boot/root images were removed after the shared filesystem reached
100%; the result and serial evidence remain).

Task 2 is closed from the current QEMU receipt. Task 3 transfer and 992/993
submission boundaries now have a runtime receipt; its 2D `RESOURCE_FLUSH`
coverage is the Task 1 scanout contract, not a separate virgl 3D command. The
next critical-path gate is the Task 5 context/submit/fence fault matrix.
