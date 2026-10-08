# Daily-use desktop and ext2 recovery: 2026-09-26 QEMU evidence

The browser-web rootfs already stages the anime wallpaper, PCManFM desktop,
LXPanel and Firefox launcher. Three focused desktop staging tests passed. This
change leaves that working desktop entry intact and hardens the recovery path
used after a development boot.

The safe-reboot helper now derives a userspace deadline 180 seconds ahead of
the kernel reboot watchdog, stops the writer units present in the selected
image, drains UID-1000 processes with bounded TERM/KILL escalation, runs sync,
and only then requests reboot. All installed `asterinas-*-evidence.service`
units are included because some run as root and write persistent logs; a
failed process scan cannot be treated as an empty process list. Each bounded
step also obeys a soft deadline ahead of the kernel watchdog. It does not
claim crash consistency after an
uncontrolled power loss. The network desktop image has no browser-web units;
a focused test proved that absent units are skipped instead of causing the
recovery helper to fail before sync. All 53 `test_debian_m5_network` tests
passed. This is a host-side helper test; a physical recovery boot has not
been repeated with this change.

The RISC-V/Sv39/SMP4 QEMU filesystem run first reproduced the missing
`readahead` syscall: 0/3 new checks passed, each returned `ENOSYS`. After
adding dispatch and validation, 5/5 `readahead` checks passed (readable file,
invalid descriptor, invalid descriptor plus negative offset, write-only
descriptor and negative offset). The
`quotactl` probe returned `ENOSYS` as expected because quotas are not
implemented. `readahead` currently matches the kernel's advisory
`POSIX_FADV_WILLNEED` no-op behavior; it does not prefetch file pages or
provide a measured speedup. The focused syscall gate passed on both RISC-V
and x86-64 QEMU; an x86-64 `make kernel` build also passed.

The independent recovery gate ran:

```sh
tools/docker/run_dev_container.sh --workspace /home/ubuntu/.codex/asterinas-main-publish -- \
  make run_kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode \
  AUTO_TEST=ext2_firefox_recovery
e2fsck -fn test/initramfs/build/ext2.img
```

The guest completed 16 cycles of Firefox-style temporary-file rename,
append, cache-file replacement and `fsync`, followed by `sync` and `/ext2`
unmount. Its terminal marker was
`ASTERINAS_EXT2_FIREFOX_RECOVERY_OK cycles=16`; the fail-closed transcript
validator passed. The host's read-only `e2fsck` returned 0 and reported
11/131072 inodes and 9004/524287 blocks. The QEMU transcript is written to
the ignored local `qemu.log` and is overwritten by later runs. The repeatable
ext2 gate now requires `e2fsck -fn` to succeed before `make run_kernel`
returns success; a corrupt-image test checks the failure path. The focused
syscall gate is `AUTO_TEST=fs_syscall_compat`; it passed all five
`readahead` checks and the `quotactl` probe independently of the broad suite.

The broad `AUTO_TEST=regression REGRESSION_TEST_DIRS='[ "fs" ]'` run did not
reach its terminal success marker. The new syscall cases passed, then the
existing `isolation/pivot_root` case failed while binding `/` to
`/second_root` with `ENOENT`. This is separate from the focused ext2 gate and
remains unresolved.

For display acceleration, the EIC7700 native scanout experiment remains
opt-in: a selected physical boot reached Xorg and Firefox at 1920 × 1080,
but HDMI pixels, visible latency and tear-free presentation were not verified.
The default is still the firmware-copy backend. There is no PowerVR render
node or measured GPU speedup; the installed RockOS `pvrsrvkm` module does not
match its running kernel. See the native-scanout gate and PowerVR inventory
documents for the exact evidence and next hardware gates.
