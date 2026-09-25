# DRM desktop on current main: scanout evidence

The live Megrez Firefox session was not restarted or altered during this work.
The candidate was built in the isolated branch `codex/drm-main-perf-20260925`.
Its DRM integration merge is `8e9b4aa16aa56ef592482ca6d6386b51c985164b`.
Both the DRM demo head `c0a65e67562132b8f112cb993a96b46962a02583`
and the wake-balancing change `53c4601a602fe11c426de38909914960c44aa890`
are ancestors of the integration merge.
The current-main `/proc/<pid>/task/<tid>/schedstat` implementation is present.

The firmware framebuffer backend now counts successful full and dirty presents
separately and records their copied bytes, cumulative elapsed time, and maximum
elapsed time.
It emits `ASTERINAS_DRM_SCANOUT` at power-of-two total present counts and,
while the screen is updating, after five seconds without a report.
The timer starts after geometry validation, before the scratch-row lock, and
stops immediately after the last framebuffer write and synchronization,
while the scratch-row lock is still held, so it includes
lock wait, VMO read, CPU copy, and required display cache synchronization.
It excludes failed presents, statistics update, and the reporting log call.
Each line includes a monotonic timestamp (`at_ns`) and successful-present
sequence number (`successes`), allowing concurrent log lines to be reordered.
The metrics do not distinguish those included sub-operations.

Four new kernel tests were run in the project container on the RISC-V QEMU
ktest adapter, each with its exact function name as the filter.
Each serial result reported `1 passed; 0 failed; 281 filtered out` and
`[ktest runner] All crates tested.`
An earlier prefix filter reported `0 passed; 0 failed; 281 filtered out`
despite a zero process exit, so that attempt is not used as evidence.
The focused tests cover full/dirty accounting, saturation and monotonic
maximum, copied-byte accounting for overlapping dirty rectangles, and
five-second snapshots after the initial exponential milestones.

The optimized `riscv64` Sv39/SMP4 Image was built with:

```sh
tools/docker/run_dev_container.sh --workspace /home/ubuntu/.codex/asterinas-drm-main-perf-20260925 -- \
  make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

Its SHA-256 is
`92e46db13dc2c23ecbdcf4b7eb67b8aa27b8a259f5cf99461e6c9c0eeeefabe7`.
The QEMU firmware gate used precisely that image and reported
`passed: true`, six stages, and `BOOT_COMPLETED` for both `megrez-basic`
(1280 × 1024) and `megrez-board-geometry` (1920 × 1080).
The serial logs show the new event at counts 1, 2, and 4.
At 1920 × 1080, four full presents copied 33,177,600 bytes in
158,846,600 ns cumulatively, or 39.71 ms per present in this QEMU run.
The corresponding 1280 × 1024 cumulative time was 92,186,700 ns for
20,971,520 bytes, or 23.05 ms per present.
These values are QEMU measurements, not physical-board timings or a Firefox
speedup claim.

The gate reused the previously compiled probe initramfs and geometry-specific
U-Boot builds because the project container's RISC-V C compiler lacks the
cross-libc headers needed to rebuild the static probe.
The kernel image, boot disk, manifests, serial logs, and result JSON were
freshly generated or checked for this run under
`/home/ubuntu/.codex/asterinas-drm-demo-20260925/target/qemu-uboot/drm-main-perf/`.
The result JSON records the same kernel SHA-256 for both device sets.

On EIC7700, firmware framebuffer writes may require the platform L3 flush
path when Svpbmt and Zicbom are unavailable.
QEMU has Svpbmt and therefore does not measure that board-specific cost.
The next controlled physical boot should keep the root serial management
channel, capture the next scanout report before and after a short Firefox
interaction (allow five seconds for each boundary), and read per-thread
`schedstat` for Firefox and Xorg.
Those two measurements will show whether copy/cache synchronization or
runnable wait is the next bottleneck to optimize.
