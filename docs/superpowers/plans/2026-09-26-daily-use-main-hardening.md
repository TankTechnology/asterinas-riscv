# Daily-use hardening on main: desktop, recovery, and syscall compatibility

The published `main` already contains the online wallpaper, PCManFM desktop,
LXPanel, and Firefox launcher (`face05c20`). Its focused rootfs staging test
passes. Keep that implementation; validate the existing short QEMU desktop gate
and change only defects reproduced there. Preserve the opt-in root debug console.

## 1. Quiesce and recover the browser filesystem

Source candidate: the uncommitted `network-stack-integration` worktree and its
`docs/superpowers/plans/2026-09-11-ext2-safe-recovery.md`. Port by topic to this
main checkout, never by merging the entire stale branch.

1. In `tools/riscv/tests/test_debian_m5_network.py`, add the three existing
   focused safe-reboot scenarios: stop writers before sync, derive a userspace
   deadline ahead of the kernel watchdog, and refuse sync/reboot while UID-1000
   writers survive. Run those three tests against the current script and record
   the expected failure.
2. Update `tools/riscv/debian/rootfs/megrez_safe_reboot.sh` with bounded unit
   stop, process drain, sync, progress markers, and a kernel-watchdog margin.
   Run the three focused tests, then the whole `test_debian_m5_network` module.
3. Port the bounded ext2 Firefox-state regression from the candidate into
   `test/initramfs/src/regression/fs/ext2/` and its focused runner. Keep the
   workload to 16 cycles and give it a standalone `AUTO_TEST` mode. Run it in
   the RISC-V QEMU ext2 fixture and check the result image with host `e2fsck`.
4. Move the automated Firefox profile and evidence to tmpfs in a separate
   image-contract change: it spans four units, the rootfs builder, the Firefox
   launcher, the Marionette gate, and QEMU evidence extraction. Keep this out
   of the recovery/syscall patch until its full browser contract tests pass.

## 2. Complete the missing syscall names without false performance claims

Source candidate: the uncommitted `megrez-desktop-only` worktree. The old
page-cache readahead optimization was rejected on measured startup performance
in `docs/performance/2026-09-19-drm-desktop-boot-performance.md`; do not
reintroduce it as part of syscall compatibility.

1. Add C regression cases for `readahead` (readable regular file succeeds,
   invalid/write-only descriptor fails) and `quotactl` (explicit `ENOSYS` until
   quotas exist) under `test/initramfs/src/regression/fs/`. Wire them into the
   existing Makefile and runner and observe their failure on current main.
2. Port the focused syscall handlers and generic dispatch registration. Make
   `readahead` an advisory no-op with validated arguments, as current
   `POSIX_FADV_WILLNEED` is; document that it does not prefetch pages. Make
   `quotactl` an explicit unsupported operation, not a claim of quota support.
3. Run the focused regression in QEMU, check RISC-V and x86-64 builds, and run
   formatting/lint checks for touched Rust and C files.

## 3. Handoff and display boundary

Record exact commands, artifact hashes, and test outcomes. The EIC7700 fixed
1920×1080 native scanout is opt-in and has a verified selected boot but no
observed HDMI-pixel or visible-latency evidence. Do not enable it by default or
claim PowerVR acceleration. The next display gate is a visible-pixel check and
bounded dirty/full-frame updates; GPU rendering requires a compatible RockOS
`pvrsrvkm` reference boot and a render-node trace.
