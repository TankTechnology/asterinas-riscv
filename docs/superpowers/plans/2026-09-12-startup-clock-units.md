# Startup clock units implementation plan

> Execute inline in the existing `codex/megrez-boot-main` worktree using
> systematic debugging and test-driven development. Preserve the qualified
> physical kernel/menu; no board deployment or firmware changes in this step.

**Goal:** Make startup CPU measurements agree between procfs, ELF auxiliary
vectors and standard libc tools, as selected in the preceding investigation.

**Architecture:** Keep OSTD's internal 1000 Hz timer untouched. Convert to
Linux's user-visible 100 Hz clock ticks at procfs output boundaries and publish
the same frequency through `AT_CLKTCK`. Do not implement unrelated syscalls,
change scheduling, or claim this itself accelerates Firefox.

**References:** Linux v6.16 [UAPI frequency](https://github.com/torvalds/linux/blob/v6.16/include/uapi/asm-generic/param.h),
[proc task output](https://github.com/torvalds/linux/blob/v6.16/fs/proc/array.c),
and [ELF auxiliary vector](https://github.com/torvalds/linux/blob/v6.16/fs/binfmt_elf.c).

## Tasks

- [x] Add `test/initramfs/src/regression/fs/procfs/clock_ticks.c`: assert
  `getauxval(AT_CLKTCK) == sysconf(_SC_CLK_TCK) == 100`; parse proc stat fields
  14, 15 and 22; compare process CPU seconds with `CLOCK_PROCESS_CPUTIME_ID`
  after bounded CPU work, compare global/per-CPU idle time with uptime, and
  bracket a freshly forked child's start time with `CLOCK_BOOTTIME` reads.
  Use the existing test framework, allowing 50 ms plus observation overhead
  for CPU quantization. Run the exact binary on Linux and on the frozen
  Asterinas kernel in a diskless QEMU initramfs; expect Linux pass and Asterinas
  unit/auxv failures before changing kernel code.
- [x] In `kernel/src/time/mod.rs`, define the sole `USER_HZ: u64 = 100` and
  saturating conversions:

  ```rust
  pub(crate) fn duration_to_clock_ticks(duration: Duration) -> u64 {
      let ticks = duration.as_nanos() * u128::from(USER_HZ) / 1_000_000_000;
      u64::try_from(ticks).unwrap_or(u64::MAX)
  }
  pub(crate) fn jiffies_to_clock_ticks(jiffies: ostd::timer::Jiffies) -> u64 {
      let ticks = u128::from(jiffies.as_u64()) * u128::from(USER_HZ)
          / u128::from(ostd::timer::TIMER_FREQ);
      u64::try_from(ticks).unwrap_or(u64::MAX)
  }
  ```

  Add kernel tests for zero, just below/at 10 ms, one second, and saturation.
  Use `duration_to_clock_ticks(clock.read_time())` for process/thread CPU and
  child/alarm fields in `kernel/src/fs/fs_impls/procfs/pid/task/stat.rs`;
  record process start time as a `Duration` from `aster_time::read_monotonic_time`
  and convert it with `duration_to_clock_ticks`. Remove that
  file's duplicated internal-jiffies conversion. Convert every CPU time
  field at output in `kernel/src/fs/fs_impls/procfs/stat.rs`, without changing
  stored counters. Set `AuxKey::AT_CLKTCK` to `USER_HZ` in
  `kernel/src/process/program_loader/elf/load_elf.rs`.
- [x] Reuse the persistent container and offline caches for compilation.
  Run the same C regression on the source-built kernel in diskless QEMU;
  require zero test failures and clean termination. Run the existing Basic
  and automatic Probe boot checks with that kernel. If offline dependencies
  or a baseline build defect prevent this, retain the exact error and do not
  deploy or claim kernel verification.
- [x] Check formatting/whitespace, perform normal review, and record commands,
  source/artifact provenance and remaining verification limits. Commit the
  scoped change only after evaluating those results; do not push or promote
  an unqualified kernel to the board.

## Results (2026-09-12)

Two defects were reproduced independently of Firefox:

1. Procfs exported internal 1000 Hz ticks while libc used 100 Hz, and ELF
   auxiliary vectors omitted `AT_CLKTCK`. The final regression fails six
   assertions on the source-built baseline. Export conversion and the auxv
   entry correct the mismatch without changing timer frequency or scheduling.
2. A stronger start-time test exposed a second defect after the unit fix:
   the child's start time was 2.180 s, outside the creation bracket
   [3.176, 3.209] s. Accumulated timer interrupts lagged the monotonic clock
   during startup. Recording the same clock source as uptime fixes this:
   the final child start is 3.430 s within [3.409, 3.451] s.

Final validation used RISC-V QEMU, SMP=4, 2 GiB RAM, no disk and no NIC:

| Check | Result |
| --- | --- |
| Static C regression, Linux reference | 11 assertions passed |
| Static C regression, final Asterinas kernel | 11 assertions passed; 6.739 s |
| Duration conversion kernel test | 1 passed, 0 failed, 216 filtered |
| Jiffies conversion kernel test | 1 passed, 0 failed, 216 filtered |
| Existing Basic boot check, final kernel | Passed; 6.802 s |
| Existing automatic Probe boot check, final kernel | Passed; 5.629 s |
| Final offline kernel build | Passed; 12 existing warnings |
| Rust/C formatting and whitespace checks | Passed |

The QEMU elapsed values describe the complete small test runs, not Firefox
startup or physical-board boot latency. The two conversion kernel tests ran
before the start-clock-source change; their helper code was unchanged, and
the final C regression exercises the start-clock-source change.

Normal independent review found no actionable defects. Its suggestion to
strengthen the original start-time upper bound led to the second reproducer.
Reaped-child CPU fields and the alarm field use the shared conversion but
do not yet have dedicated user-space assertions.

### Reproduction and local evidence

All compilation used the existing persistent container with offline caches.
OSDK was built in place, not installed or downloaded:

```sh
OSDK_LOCAL_DEV=1 cargo build --manifest-path osdk/Cargo.toml --locked --offline --bin cargo-osdk
cd kernel
OSDK_TARGET_ARCH=riscv64 OSDK_LOCAL_DEV=1 ../osdk/target/debug/cargo-osdk osdk build \
  --scheme riscv --features riscv_sv39_mode \
  --initramfs ../target/startup-clock-units/after-reviewed/initramfs.cpio
```

The C test is automatically discovered by the existing regression Makefile.
For this focused run it was compiled statically with the cached Nix RISC-V
GCC/glibc toolchain (`-Wall -Werror -O2 -static -lpthread`) and added to an
existing Stage1 initramfs. No Debian/Firefox image rebuild was needed.

Local, ignored artifacts under `target/startup-clock-units/` retain:

- `before-reviewed/`: final regression against the source-built baseline.
- `after-reviewed/`: unit fix still failing the stronger start-time check.
- `final/`: final regression serial log, invocation, hashes and result.
- `basic-final/`, `probe-final/`: existing startup-check results and logs.
- `ktest-duration.log`, `ktest-jiffies.log`, `build-final.log`.
- `run_qemu.py`: one-off diskless test runner, not a new production boot tool.

Run the existing startup checks from the repository root in the container:

```sh
python3 -m tools.riscv.megrez_menu_qemu \
  --kernel target/startup-clock-units/kernel-final.Image \
  --initramfs target/megrez-menu/candidate-2/stage1-d62ab8325e03.cpio \
  --mode basic --output target/startup-clock-units/basic-final
# Repeat with --mode probe-auto and a separate output directory.
```

Artifact SHA-256 values:

- Source baseline kernel: `b405de04dd51ed094a45d290a323059a64561b90a3f814b927209bb9e81c3d41`.
- Final kernel: `03786f0bfaadd016b230d24d797e8e1b5972b58475eb8a90ee2886dd3c695404`.
- Final regression initramfs: `20cc3676b0bd56e0a660add50ceebe82a551a7d859dfa4cef6f650ec598e0742`.
- Unmodified startup-check Stage1: `d62ab8325e03ec9959a82336ad7ddab2d433d9e6dfc1006d6237fa9f6c80c1e0`.

Harness corrections are not kernel failures: the first temporary init script
omitted creating `/proc` before mounting it; this was corrected before the
meaningful baseline run. An initial module-name ktest filter selected zero
tests; only subsequent exact-function-name runs are counted above. The
temporary diskless ktest OSDK configuration was removed after testing.

### Boundaries and next step

Read-only SSH confirmed the board was accessible in RockOS (Linux 6.6.87,
root `/dev/mmcblk1p3`). This change did not reboot or deploy to it, modify its
menu, or change firmware. There was no full Desktop/Firefox experiment.

This fixes measurement correctness, not Firefox's remaining startup delay.
The next performance investigation can now compare CPU consumption and
process creation against a consistent startup timeline before separating
file-loading, memory-management and scheduling/waiting costs. Other procfs
placeholders and per-thread start-time semantics remain outside this change.
