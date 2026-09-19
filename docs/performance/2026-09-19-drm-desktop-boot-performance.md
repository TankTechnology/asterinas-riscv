# DRM desktop boot performance: the gap was the build profile, 2026-09-19

**Headline: built optimized, Asterinas brings the Debian desktop up in 15--16
seconds against the Linux control's 15--19, and is ahead of it at every
milestone along the way.** The ~20x gap recorded earlier the same day was
almost entirely an artifact of measuring an unoptimized kernel.

This record supersedes the debug-era numbers it replaces, states why they
misled, and keeps the one optimization that survived on its own merits.

## Step 0, which was skipped: confirm what you are comparing

`Makefile` has:

```make
RELEASE ?= 0
RELEASE_LTO ?= 0
```

Both default to off, so a plain `make kernel TARGET_ARCH=riscv64 ...` builds
`dev profile [unoptimized + debuginfo]` -- a ~16 MB image. With `RELEASE=1` it
is ~6 MB. The CI release workflow uses `make iso RELEASE=1`; the RISC-V desktop
gate is not in CI and had never been run against an optimized kernel.

Every measurement taken before this was found -- the 20x desktop gap, the
per-operation benchmark, and the ASID, TLB-flush and path-resolution analysis
built on top of them -- was comparing a `-O0` kernel against `-O2` Linux.

## Result

Same Debian 13.6 rootfs, same QEMU/TCG (`-smp 4`), same in-guest evidence
script for both kernels. Asterinas is `RELEASE=1`; the Linux control is the
Debian `6.12.107+deb13-riscv64` kernel. Guest uptimes in seconds:

| milestone | Asterinas (release), 3 runs | Linux control, 3 runs |
|---|---|---|
| `basic.target` | **3, 4, 3** | 7, 7, 8 |
| Xorg first seen | **6, 6, 6** | 10, 11, 12 |
| openbox first seen | **9, 10, 11** | 15, 15, 17 |
| all five clients running | **11, 11, 11** | 15, 17, 18 |
| desktop READY (wall clock) | **16, 15, 16 s** | ~15--19 s |

The `xorg -> openbox` gap, which was 87--162 s before and the single largest
cost in the boot, is now **3--5 s** against Linux's 3--6 s.

## The per-operation benchmark, before and after

Run on an otherwise-idle guest (desktop session masked) so the numbers describe
the kernel rather than its load:

| operation | debug | release | Linux | release vs Linux |
|---|---:|---:|---:|---:|
| `alu` (userspace, control) | 1.55 ns | 1.17 ns | -- | -- |
| `clock_gettime` (vDSO, control) | 0.298 us | 0.210 us | -- | -- |
| `fork+exec+wait` | 423 ms | **8.12 ms** | 9.98 ms | **0.81x (faster)** |
| `getpid` | 189 us | **6.39 us** | 1.50 us | 4.3x |
| `fstat`(fd) | 285 us | **7.28 us** | -- | -- |
| `stat`(path) | 1631 us | **18.1 us** | -- | -- |
| `open+close` | 1792 us | **34 us** | 24 us | 1.4x |
| `readlink`(/proc) | 4184 us | **45.5 us** | 18.5 us | 2.5x |

The two control rows are what made this findable. They never enter the kernel,
they ran on the same Debian binaries under the same emulation, and they were
fine throughout -- so the kernel was the only place the cost could be.

## Why the earlier number looked like an architecture problem

The debug penalty is **uniform**. Two unrelated phases -- systemd reaching
`basic.target`, and Xorg plus five clients coming up -- were slower by a similar
~20x, and every kernel operation was slower by a similar factor.

That shape is ambiguous, and it was read the wrong way here. A *uniform*
slowdown means a constant factor: build configuration, or a fixed per-syscall
cost. A *phase-specific* slowdown means a missing feature. The reasoning that
followed from reading it as "the kernel is architecturally slow" produced a
long hunt for a missing feature, including an ASID and TLB-flush investigation,
none of which was the problem.

The check that would have caught it in a minute is comparing the size of the
kernel image, or reading the profile line the build already prints.

## Kept: removing redundant boot work

`ldconfig.service` (~48 s) and `systemd-journal-catalog-update.service` (~31 s)
top `systemd-analyze blame`. On this rootfs both are redundant rather than
merely expensive: the image is immutable, nothing installs libraries at
runtime, and it already ships a complete `/etc/ld.so.cache` (15,771 bytes).

**Re-measured on the release kernel, it is worth about one second and nothing
end-to-end.** Interleaved A/B, two runs per arm:

| | unmasked | masked |
|---|---|---|
| `basic.target` (guest uptime) | 5, 5 | 4, 4 |
| desktop READY (wall) | 16, 19 s | 16, 18 s |

The mask is an *absolute* saving, and on the debug kernel -- where the same
`ldconfig` run cost far more -- it was worth ~36 s of a ~350 s boot. On a
16-second boot the same work costs about a second, and the wall-clock
difference is inside the noise.

So the earlier framing of this as "the landed optimization" does not survive
the release kernel: what looked like a 10% win was 10% of a debug-inflated
boot. It is kept because the work is genuinely redundant -- an immutable image
that already ships a complete `/etc/ld.so.cache` has no reason to rebuild it
every boot -- and not because it is a meaningful speedup.
`tools/riscv/perf/toggle-redundant-units.sh` applies it.

The masks must live in the base image: the gate recomputes a derived image's
hash from (base, spec), so a symlink added to a derived image is rejected with
`reason: validate`, and `dev_overlay` cannot create symlinks at all. `debugfs`
does not reproduce a byte-identical image, so each toggle re-syncs
`root_image_sha256` in the base manifest.

## Not landed: page-cache readahead

`BackedVmo::commit_range` batches a page range into one `IoBatch` and ext2
already merges adjacent requests, but nothing ever asked for adjacent pages, so
a sequential reader paid one backend round trip per page.

A first version broke the ktest `fault_around_reads_only_the_mapping_window`,
which asserts that a fault reads *exactly* the mapping window -- a deliberate
invariant, not a bug. The test also supplied the fix: the fault path already
asks for a 16-page window, so scoping readahead to narrower requests leaves the
invariant intact and targets the single-page buffered read.

**Reverted, and re-measured on the release kernel to be sure.** The first
measurement was on the debug kernel, where its effect on `basic.target`
(123--131) overlapped the no-readahead runs (129--139) and the end-to-end
figures were indistinguishable -- so it could not be shown to help, but the
noise that hid it might have been the debug kernel's. On the release kernel the
answer is the same and the runs are tight enough to say so:

| | desktop READY | `basic.target` |
|---|---|---|
| no readahead (3 runs) | 16, 15, 16 s | 3, 4, 3 |
| with readahead (2 runs) | 17, 17 s | 4, 4 |

It is, if anything, slightly slower. The patch is preserved at
`docs/performance/2026-09-19-readahead-rejected.patch`; it is rejected on
measurement twice over, not on noise.

## Corrected claims

Three earlier results did not survive checking and are withdrawn:

- **The microbenchmark's absolute values were load-confounded** and, separately,
  taken on a debug kernel. The idle re-measurement fixed the first problem; the
  build profile was the second.
- **"Readahead cut host read syscalls 33%"** was wrong. `/proc/<qemu-pid>/io`
  counts every read the QEMU process makes, including polling its `-stdio`
  chardev; later runs showed 1,047,925 "reads" at ~100 bytes each, which cannot
  be disk I/O. Only `read_bytes` is usable, and only when `rchar` agrees with it.
- **A suspicion that `ACTIVATED_VM_SPACE` was a shared global** was wrong; it is
  `cpu_local_cell!` and per-CPU.

## Why ASID cannot be measured here

Worth recording because it is a dead end that looks promising. QEMU's RISC-V
soft TLB is not tagged by ASID, so it must flush the whole TLB on any `satp`
change; guest ASIDs therefore change nothing. Commit `1e0d985fa9` (2019) made
that flush conditional on the ASID, and commit `5242ef887` (2022) reverted it,
noting that QEMU "doesn't currently exploit ASIDs for translation performance".
Both kernels pay the same flush cost here, so the mechanism cannot explain a
difference between them.

## Boundaries

- The Asterinas runs have the two redundant units masked; **the Linux control
  does not**. This compares our best configuration against a stock Linux, which
  is the fair question for "can we reach Linux", but it is not an identical
  configuration. On a boot this short the mask is worth a small fraction of a
  second.
- Three runs each. The gate has a documented ~25% flake rate, so three
  consistent runs is good evidence but not a long series.
- **The GPU transport still differs and cannot be matched.** The Debian
  `virtio_gpu` driver returns `EIO` for QEMU's virtio-MMIO GPU, so Linux is
  given `virtio-gpu-pci` while Asterinas binds MMIO. Disk, input, CPU count,
  memory and emulation are matched.
- These runs are not *passing* gate runs. The gate reports `reason: protocol`
  on every run including the historical baselines, and `result.json` carries an
  empty `screenshot`, consistent with its screendump step failing under
  `-display none`. The desktop itself demonstrably comes up -- all five clients
  are observed running -- but that gate verdict is a separate, unfixed issue.

## Open items

- Decide whether the RISC-V desktop gate should build with `RELEASE=1` by
  default, and whether its absence from CI is why this went unnoticed. The
  measurement tooling now refuses a debug kernel image, but the gate itself
  still takes whatever `target/osdk/aster-kernel-osdk-bin.Image` holds.
- The gate's `reason: protocol` verdict under `-display none`. `result.json`
  carries an empty `screenshot`, and the gate reports the same verdict on the
  historical baselines, so this is a gate defect rather than a run failure --
  but it means no run in this series is a *passing* run.
- What is left of the gap to Linux is microseconds per syscall (`getpid` 4.3x,
  `open+close` 1.4x) and does not move the desktop boot. `fork+exec` is already
  0.81x, i.e. faster than Linux. Whether closing the rest is worth the effort
  is a judgement call, not a measurement question.
