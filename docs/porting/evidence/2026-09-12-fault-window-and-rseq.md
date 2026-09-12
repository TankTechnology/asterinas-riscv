# Fault-window and rseq safety validation

## Established defects and repairs

Commit `830c91a68` corrects a byte/page-unit mismatch in file fault-around.
The real fault-handler regression uses fresh 128-page VMOs with offsets
0, 8 and 120 pages, and the existing 16-page read window. Before the fix,
the offset-8 case read pages 8 through 127 instead of 8 through 23. After
the fix all three exact ranges, read-only mappings and initialized data pass.
No window-size, cache-coherency or DMA policy was changed.

Commit `ba4e8f372` removes an invalid rseq implementation. It had accepted a
32-byte region but written four signature bytes at offset 32, reported CPU 0
for every thread, and advertised registration without a restart protocol.
The syscall now returns `ENOSYS` without accessing user memory; obsolete
registration storage and exit-time writes are removed. This is a safe
unsupported interface, **not implementation of restartable sequences**.
See the [Linux rseq UAPI](https://github.com/torvalds/linux/blob/v6.12/include/uapi/linux/rseq.h).

The userspace canary test independently reproduced a successful registration
with corrupted bytes outside the requested region (`RSEQ_RESULT=134`). The
same test passes after repair, including a new pthread, repeated requests,
unregister, invalid flags, malformed pointers/lengths and inaccessible memory.
Normal Asterinas regression builds default to strict `ENOSYS` assertions;
Linux retains a portable bounds check. Native Linux passes with libc's own
registration both enabled and disabled.

## Reproducible candidates

All are offline release builds with `riscv_sv39_mode`, using the persistent
container and existing Stage1/DTB/Debian image. No image or toolchain download
was needed. Only content-addressed canary files were staged over RockOS.

| Candidate | Image SHA-256 |
| --- | --- |
| Previous release | `be9c00f58ba0ce7dee681964bfc787ffffdcbd2e1ede1d59fbc2a2b9135090ba` |
| Fault-window only | `35ad2e08d1016dc36e87789672bfebd5786875df8b2f0ff8d88623e681a7d207` |
| Fault-window + rseq safety | `b7510cc3b2a41a203b53b7697db4b854a0cbbed8b72707537fc3b39c2885f675` |

The fault-only and combined images are 5,895,368 and 5,894,216 bytes.
Stage1 remains `d62ab8325e03ec9959a82336ad7ddab2d433d9e6dfc1006d6237fa9f6c80c1e0`;
DTB remains `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`.
Frozen ELF files, bundles, build logs and manifests accompany both candidates.

## Verification

- Eight selected kernel tests pass: all six mapping tests and the sequential
  batch/concurrent page-cache fault tests. Each retained successful run
  selected one test. Two earlier filters selected zero tests and are not
  counted; early fixture compile/missing-disk attempts are not behavioral REDs.
- Both release candidates pass diskless QEMU Basic, automatic Probe and
  all 11 clock/procfs assertions.
- Rseq RED/GREEN with strict default mode uses identical initramfs hash
  `386d697fd8e86954a535015316ac37759fb4e761df3ea971c070c1bd6ef7aac8`.
  Both QEMU processes reboot normally, but the pre-fix guest test fails;
  QEMU exit zero alone is explicitly insufficient.
- Rust formatting, whitespace and 73 host boot-menu/serial tests pass
  (`target/rseq-safety/host-menu-tests.log`).
  Builds retain existing warnings; no full lint or all-kernel-suite pass is claimed.
- Ordinary spec and code-quality review found no blocking issue. The review's
  strict-default test suggestion was implemented and revalidated.

## Physical observations

On the fault-only candidate, menu-to-ready was 2.232 s for Basic and 2.176 s
for Probe. Both recovered to fresh firmware. Desktop root console appeared
at 10.007 s; the first visible Firefox window was observed at 58.516 s.
The previous release observations were 74.004 and 69.906 s. These are
non-randomized fresh-home boots with the same offline/basic-only browser
policy and roughly ten-second polling, not first-paint timings or a
statistically established speedup.

The local HTML/JavaScript assertion, DOM click handler and live 1920x1080
framebuffer PNG capture passed. The content script took 6.808 s **after**
host preparation and window detection; this is not exec-to-content latency.
The guest timeline records X socket at 12.120 s and Firefox exec at 12.180 s.
`/proc/[pid]/io` is unavailable, so per-process disk-byte attribution remains
an observability gap.

The fault-only desktop recovered to fresh firmware and a logged-in RockOS
console after one software reboot request. SSH and default-menu hashes were
then verified; no extra reboot/reset was sent during the quiet interval.

On the combined candidate, root console appeared at 10.524 s and the first
visible Firefox window at 60.856 s. Local HTML/JavaScript, DOM click and
1920x1080 framebuffer capture passed again (content script 7.562 s). A separate
Python/ctypes userspace probe exercised the actual RISC-V syscall from both
the main thread and a new thread: six request forms each returned `ENOSYS`,
with every byte of the guarded buffer unchanged (`RSEQ_BOARD_PASS`). This
physical probe is distinct from the more extensive C/QEMU regression.

The combined desktop PNG was transferred with length/hash framing, decoded
and visually inspected: 41,508 bytes, 1920x1080, SHA-256
`8ee06cdcc3e088ba707c87589b2ca9eccc963a4ae2b53533d014b15cdb44fb74`.
It shows the local page's `INTERACTION PASS` result. Identical pixels to the
earlier baseline are expected for this deterministic page; capture was freshly
performed on this boot and is retained as `target/rseq-safety/desktop.png`.

The combined candidate recovered after one software reboot request: sync
acknowledgement at 0.687 s, fresh firmware prompt at 106.950 s after the reboot
request, and completion with RockOS login at 133.748 s including the initial
query. These timings are retained in `target/rseq-safety/recovery.log.json`.
Recovery is successful but still slow; the quiet shutdown interval remains a
separate diagnosis target, not a fixed problem. No second reboot or physical
reset was issued.

Final SSH verification reports RockOS 6.6.87 and no mount of Debian partition
2. `e2fsck -fn /dev/mmcblk1p2` completes with exit zero (19,505/131,072 files,
231,205/524,288 blocks). The installed kernel hash remains
`485b9079c204bf6b34055f5e1061f3011381557d8cc4b4bf2d1e4831922058c1`;
the default Asterinas and vendor menu hashes remain, respectively,
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26` and
`eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.
See `target/rseq-safety/final-rockos-check.log`. No default-menu promotion or
installed-kernel replacement was performed.

## Missing-interface audit and limits

The earlier `firefox-final` physical log has 120 syscall-272 (`kcmp`) warnings
and one syscall-428 (`open_tree`) warning. Older physical logs additionally
contain 213 (`readahead`) and 60 (`quotactl`); syscall 60 is also observed in
this fault-only desktop's current dmesg. Existing messages omit caller and
arguments, so these counts cannot establish Firefox attribution or cost.

`kcmp` needs permission-checked namespace-aware thread/resource comparison.
`open_tree` needs mount-handle semantics. A real `readahead` requires an
inode-level hook preserving truncate/invalidation synchronization and
overlayfs forwarding without copy-up; obtaining a detached page cache and
calling a fault helper is not sufficient. These interfaces remain unsupported.
They are not replaced by success stubs in this change.

The glxtest `ManageChildProcess failed` message is not by itself proof of a
timeout: [Firefox ESR140's handler](https://github.com/mozilla-firefox/firefox/blob/esr140/widget/gtk/GfxInfo.cpp)
also reports failure for a nonzero child exit. Earlier standalone glxtest
measurements returned 1 quickly under the existing GPU-avoidance policy.
The printed Firefox timestamp must not be treated as helper duration.

Local raw evidence: `target/fault-window/`, `target/firefox-fault-window/`,
`target/rseq-safety/`, and `target/startup-clock-units/{fault-window,rseq-safety}-clock/`.
The installed networking kernel and default menus are intentionally unchanged;
this canary is not network integration or normal Internet browsing acceptance.
