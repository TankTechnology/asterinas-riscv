# DRM desktop boot performance against a Linux control, 2026-09-19

This record measures how long the Debian desktop takes to come up under
Asterinas and under the real Debian RISC-V kernel.  It reports one change that
was kept, one that was implemented, measured and then reverted because it could
not be shown to help, and two earlier results that did not survive checking.

## What is being timed

Both kernels boot the same Debian 13.6 rootfs image, with the same QEMU/TCG
configuration and the same in-guest evidence script
(`tools/riscv/debian/rootfs/desktop_drm_evidence.sh`), which emits milestones as
it goes:

- `DEBIAN_DESKTOP_DRM_BOOT phase=basic-target uptime=N` -- systemd reached
  `basic.target`, timestamped on the guest's own monotonic clock.
- `DEBIAN_DESKTOP_DRM_LAUNCH xorg=... openbox=... pcmanfm=... lxpanel=... xterm=...`
  -- the guest uptime at which each desktop component was first seen running.
- `DEBIAN_DESKTOP_DRM_READY` -- all five components are running.

Because the milestone times come from the guest's own clock, they are
comparable across runs without depending on host-side timing.  Wall time
("desktop ready") is additionally measured from the QEMU process's real start,
read out of `/proc/<pid>/stat`, to the READY marker (`tools/riscv/perf/measure-desktop.sh`);
timing from a wrapper's own start would include the gate's 1 GiB image copy,
which is not boot time.

## The control, and where it is not a control

The Linux side (`target/linux-control/run-control.sh`) boots the same image
under the Debian `6.12.107+deb13-riscv64` kernel with the device set copied from
the gate's own argv.

One difference cannot be removed: **the GPU transport**.  The Debian
`virtio_gpu` driver returns `EIO` for QEMU's virtio-MMIO GPU, so Linux is given
`virtio-gpu-pci` while Asterinas binds the MMIO device.  The GPU transport is
therefore the one part of the hardware that differs, and any comparison has to
say so.  Everything else -- disk, input, CPU count, memory, emulation -- is
matched.

## Result: both phases are slower by a similar factor

| | Linux control | Asterinas |
|---|---:|---:|
| `basic.target` (guest uptime) | 6--8 s | 122--178 s |
| desktop READY | 15--19 s | 299--389 s |
| xorg -> openbox gap | 3--6 s | 87--162 s (masked) / 34--247 s (all runs) |

The two phases are different workloads -- one is systemd and the kernel, the
other is Xorg, Mesa and five clients -- and they are slower by a similar factor
(~18--25x).  That uniformity is the main finding: it rules out a single missing
feature as the explanation.  In particular, absent readahead would only have
affected the I/O-heavy phase, and it does not account for systemd's own cost.

The `xorg -> openbox` gap is the largest single block, and it is also the
noisiest: across all recorded runs it has ranged from 34 s to 247 s, so most of
the spread in "desktop READY" between runs is this gap rather than anything the
configuration changed.

## Kept: removing redundant boot work

`ldconfig.service` (~48 s) and `systemd-journal-catalog-update.service` (~31 s)
appeared at the top of `systemd-analyze blame`.  On this rootfs both are
redundant rather than merely expensive: the image is immutable, nothing installs
libraries at runtime, and it already ships a complete `/etc/ld.so.cache`
(15,771 bytes).  Rebuilding that cache at every boot is repeated work whose
result is already on disk.

The masks must live in the base image: the gate recomputes a derived image's
hash from (base, spec), so a hand-added symlink in a derived image is rejected
with `reason: validate`, and `dev_overlay` can only write regular files -- it
cannot create the symlinks a systemd mask requires.  `debugfs` does not
reproduce a byte-identical image, so each toggle also has to re-sync
`root_image_sha256` in the base manifest
(`tools/riscv/perf/toggle-redundant-units.sh`).

Verified effective: the masked runs contain 0 `ldconfig` mentions, against
9--127 in the unmasked logs.

| Configuration | `basic.target` uptime |
|---|---|
| unmasked | 163, 178 (and 152, 154, 167, 191, 236, 238, 261, 477 historically) |
| masked | **129, 136, 139** |

Every masked run is below every unmasked run, including all historical ones.
The improvement is ~36 s at this milestone.  Note the honest boundary: the
end-to-end desktop time does *not* show this clearly (masked 325 and 381 s
against unmasked 362 and 386 s), because the desktop phase's own variance is
larger than the gain.

## Not landed: sequential readahead in the page cache

`BackedVmo::commit_range` (`kernel/src/vm/page_cache/vmo/mod.rs`) already
batches a page range into one `IoBatch`, and ext2's `InodeBlockManager` already
overrides `submit_read_bios` to merge adjacent pages into one device operation.
Nothing ever asked for adjacent pages, though: callers request exactly the pages
one `read()` touches, so a sequential reader paid one backend round trip per
page with nothing to merge.

The change widens the fetched range by `READAHEAD_PAGES` (16 pages, 64 KiB) and
trims the result back to what the caller asked for.

Two design points are load-bearing:

- **Sequential detection is stateless.**  The window is widened only when the
  page immediately before the range is already resident
  (`Vmo::is_page_resident`), which is a direct signal that something just walked
  forward from there.  Scattered access reads exactly what it asked for, and
  there is no per-file cursor to keep or to get wrong.  A `BackedVmo` is a
  short-lived wrapper, so state would have had nowhere natural to live anyway.
- **The bound is exact, not approximate.**  ext2 rejects a *whole batch* if any
  request is out of bounds (`block_manager/mod.rs`), so over-reading past the
  object would turn a working read into an error.  `npages` is
  `new_size_bytes.div_ceil(PAGE_SIZE)` and `page_cache.resize` rounds up to page
  boundaries, so the page-aligned VMO size *is* `npages` -- the clamp is the
  same one `end_idx` already carried.

### A first version was rejected, and why

The first version applied readahead to every path.  It broke an existing ktest:
`fault_around_reads_only_the_mapping_window`
(`kernel/src/vm/vmar/vm_mapping.rs`) asserts that a page fault reads *exactly*
the mapping window `offset..offset+16`.  That is a deliberate invariant -- fault
around, not "read the file" -- and widening it was overriding a decision rather
than fixing a bug.

The test also supplied the fix.  The fault path *already* asks for a 16-page
window, so it is already batched and gains nothing from readahead; the unbatched
path is the single-page buffered read.  The scoped version therefore only widens
requests narrower than `READAHEAD_PAGES`, which leaves the fault window exactly
where the invariant says it should be and needs no new parameter.  That version
is correct; it is reverted on the evidence, not on the design.

| Configuration | `basic.target` uptime | mean |
|---|---|---:|
| unmasked | 163, 178 | 170.5 |
| masked, no readahead | 129, 136, 139 | 134.7 |
| masked + readahead (first version) | 122, 126, 128 | 125.3 |
| masked + readahead (kept version, scoped) | 123, 131, 131 | 128.3 |

**It was reverted, because the effect did not survive the scoping.**  The first
version's separation (122--128 against 129--139) came from a version that was
wrong for an independent reason.  Once readahead was confined to the unbatched
path, the range moved to 123--131, which overlaps the no-readahead runs at 129
and 131, and the mean gained only ~6 s against a within-arm spread of ~6--10 s.
Desktop READY tells the same story: 301 / 317 / 386 s with the scoped version
against 325 / 381 s without, means 335 against 353, with individual runs from
every configuration landing inside the same 299--389 s band.

Six readahead runs and three no-readahead runs are not enough to separate a
~5% shift from this machine's run-to-run variation, and an optimization that
cannot be shown to help does not belong in the kernel.  The change is preserved
as `docs/performance/2026-09-19-readahead-rejected.patch` rather than discarded,
because the reasoning behind it is sound and the measurement it needs is cheap:
run more arms, or re-measure with the desktop-phase variance removed.

## Corrected: two earlier results that did not survive checking

**The microbenchmark is confounded.**  The per-operation numbers previously
recorded (`stat` 387x, `getpid` 117x, `open+close` 114x, `fork+exec` 37x) are not
trustworthy.  `boot-bench` runs synchronously at the top of the evidence script
at `basic.target`, while systemd starts the desktop units in parallel -- so on
Asterinas the benchmark competes with a ~100 s llvmpipe/Xorg startup for the
four vCPUs, and on the Linux control the desktop settles in ~3 s.  The
asymmetry inflates the Asterinas figures by an unknown amount.  These numbers
should not be used to choose an optimization until the bench is re-run with the
desktop suppressed on both sides.

**The host read-syscall counter is contaminated.**  Readahead was first reported
as cutting host read syscalls from 138,405 to 92,721 for the same bytes.  Two
later runs showed `syscr` of 1,047,925 and 1,340,904 at roughly 100 bytes per
read, which cannot be disk reads.  `/proc/<pid>/io` counts every read the QEMU
process makes, including a non-disk source -- almost certainly QEMU polling its
`-stdio` chardev.  Only one run had `rchar` captured alongside, and only there
did `rchar` (120 MB) track `read_bytes` (129 MB).  `syscr` is not a usable proxy
for request count here; the claim is withdrawn.

## Open items

- The gate reports `reason: protocol` on every run, including the historical
  baselines.  `result.json` carries an empty `screenshot`, consistent with the
  gate's screendump step failing under `-display none`.  This is uniform across
  arms, so it does not bias the comparison above, but it means these runs are
  not passing runs and should not be cited as such.
- Re-measure the microbench with the desktop suppressed on both kernels.
- The largest single cost, the xorg -> openbox gap, is not yet explained.  Xorg
  is first seen running at 190--260 and openbox only appears at 277--361, and
  Xorg's own log does not reach the serial console, so what fills that gap is
  still unknown.  The gap is also where most of the run-to-run spread lives,
  which is why an improvement smaller than it cannot be resolved from these
  runs.
