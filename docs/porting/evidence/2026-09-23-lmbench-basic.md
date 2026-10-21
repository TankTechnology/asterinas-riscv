# Basic LMBench on RISC-V Asterinas

All 18 selected LMBench checks pass on the current RISC-V Asterinas release
kernel in QEMU, using the repository's pinned LMBench package.
The reusable guest runner takes 8.417 seconds in the successful run,
excluding VM startup and runtime staging.
No kernel or LMBench source change was needed.
This is compatibility evidence, not a full LMBench suite or a performance comparison.

## Scope and build

The package comes from `test/initramfs/nix/benchmark/lmbench.nix`:

- Source: `asterinas/lmbench`, revision
  `afb47eddaf10a411c1ea3cb64965461f1308a6ea`.
- Source NAR hash: `sha256-XFcvWwqOci1jdNaBGj7/svCT49JAppYvN+ABeO9rqbw=`.
- Nix derivation version label: `0.1.0`.
  This is the repository package label, not an upstream LMBench release claim.
- Output: `/nix/store/nx5i8n4dvz49qbh2gglzlnbvqsd9qfxp-lmbench-riscv64-unknown-linux-gnu-0.1.0`.
- Runtime: original Nix-built ELF files and their complete seven-path Nix closure,
  including RISC-V glibc 2.40 and libtirpc 1.3.6.

Build only the required package rather than all benchmark applications:

```sh
tools/docker/run_dev_container.sh --workspace /absolute/worktree -- \
  nix-build test/initramfs/nix/default.nix --argstr target riscv64 \
  -A benchmark.lmbench --out-link /root/asterinas/target/lmbench-riscv64
```

The persistent container's direct GitHub download timed out,
while dependency downloads from the Nix cache succeeded.
Fetching the same revision from GitHub's codeload endpoint through the existing
local proxy succeeded; importing it into Nix verified the pinned source hash.
No proxy service configuration or source revision was changed.
The package then built successfully using the existing derivation.
Build warnings, including the upstream optional `bk` command and non-ELF fixup
messages, are preserved in `nix-build-retry.log` in the raw archive.

The selected binary architecture, runtime closure, and individual ELF hashes
are recorded in `bundle.json` and `smoke-result.json`.
The 14,754,669-byte compressed runtime bundle has SHA256
`16bf397f903a62b1fb91520f3614887021833af235c5368977b8ef50444e4e0c`.
The bundle is staged into a disposable copy of the Debian root disk,
then verified by SHA256 inside the guest before extraction.
Binaries are not relinked, patched, or replaced by mocks.

## Successful run

`qemu-02` uses QEMU RISC-V virt, TCG, four CPUs and 2 GiB RAM.
Its kernel contains the preceding cgroup-notification stability fix.
The desktop is stopped before the benchmark phase.

- Kernel base commit: `7e9dcc0cbd2055fe66ec4a08c5d37d237b1a5d95`.
- Kernel Image SHA256:
  `05bc7a85538ab246d96ab87ea67f22d767dbfc93fa5a895e9a6f1c19d9418ad0`.
- Input root SHA256:
  `bd855c9855e6734cb5e154d488d7c1899f9c1949ac01dae87cd0fc88702e35cf`.
- Guest boot ID: `d252f587-daa1-4daf-b372-065264654e49`.
- Fresh successful root serial command witnesses: 40, all with the same boot ID.
- Complete guest runner: 18 cases, all return zero with finite positive results;
  host QEMU result also passes.

| Category | Checks | Outcome |
| --- | --- | --- |
| Syscalls | getppid (`null`), read, write, stat, fstat, open/close | 6/6 pass |
| Processes | fork+exit, fork+execve, fork+shell | 3/3 pass |
| IPC and scheduling | pipe latency; two-process, zero-working-set context switch | 2/2 pass |
| Signals | handler installation; signal catch | 2/2 pass |
| Memory bandwidth | 1 MiB `rd`, `wr`, `cp` | 3/3 pass |
| Virtual memory | 1 MiB file mmap; file-backed page faults | 2/2 pass |

Every case uses `-P 1 -W 0 -N 1` and `ENOUGH=10000`.
Loop and timer overhead calibration remain enabled;
inherited overrides for those calibrations and scheduler selection are cleared.
[LMBench's pinned timing code supports the ENOUGH override](https://github.com/asterinas/lmbench/blob/afb47eddaf10a411c1ea3cb64965461f1308a6ea/src/lib_timing.c).
Each process group has a ten-second deadline and at most two seconds to collect
output after a timeout kill; the runner stops at the first failed case.
This keeps the normal test small without disguising a timeout as a valid number.

`lat_proc` ignores individual child exit status internally.
The runner therefore verifies `/tmp/hello` independently through both direct
execution and `/bin/sh -c`, with an empty environment, before measuring exec.
It never overwrites a different existing `/tmp/hello` and removes the file only
when it created it.
This preflight detects missing executable/runtime dependencies;
it does not instrument every exec inside LMBench's measurement loop.

All raw measurements, units and commands are retained for troubleshooting.
Single repetitions under TCG, a small working set, and shortened calibration
intervals are insufficient for performance rankings or regression thresholds.
In particular, the printed memory throughput is not evidence of board bandwidth.
Networking, Unix sockets, filesystem throughput, memory-latency sweeps,
and the full comparative suite are outside this first acceptance set.

## Reusable runner and regression checks

`tools/riscv/lmbench_smoke.py` runs inside a guest with Python 3.10 or newer:

```sh
python3 /path/to/lmbench_smoke.py \
  --bin-dir /nix/store/<lmbench-package>/bin \
  --output /tmp/lmbench-smoke.json
```

The JSON includes the exact command, status, output, parsed value, unit,
timeout flag, duration and binary hashes.
Exit zero alone is insufficient: the expected output format and a finite
positive measurement must also be present.
An error or incomplete run leaves `passed=false`.

The first prototype run (`qemu-01`) passed 13 cases and then incorrectly rejected
memory read's valid `1.05 12419.34` output.
The temporary parser expected the size field to be `1.00`.
LMBench prints a 1 MiB buffer as rounded decimal MB (`1.05`) for `bw_mem`,
and as `1.048576` for `lat_mmap`.
The production parser handles both, checks the size, and rejects extra error text.
This prototype failure is a runner bug, not an Asterinas failure.

Eight focused unit tests cover decimal-MB parsing, context-switch headers,
exact result labels, invalid/missing results, stderr results, nonzero exits,
and termination of a forked child holding the output pipe open.
They all pass in the persistent dev container:

```sh
python3 -m unittest -v tools.riscv.tests.test_lmbench_smoke
```

The successful QEMU run executes the actual tracked runner source,
not the earlier prototype.

## Evidence and QEMU replay

The adjacent `2026-09-23-lmbench-basic/` directory contains the successful
structured report, both QEMU results, normalized observations,
bundle metadata, the unit-test log and a SHA256-indexed raw archive.
Normalization removes byte-identical repeated `LIFECYCLE` records only.
The archive preserves both delivered guest programs and build/serial logs.
Large disk images and the Nix runtime bundle are not committed.

For the original local layout, restore the archived top-level `run.py`,
`guest.py`, `bundle.py` and `bundle.json` under
`/root/asterinas/target/lmbench-basic-20260923/` inside the main persistent container.
After building the fixed Nix package, regenerate its runtime tarball with
`bundle.py`; it also refreshes `bundle.json` if archive bytes differ.
The QEMU adapter reads the existing
`target/qemu-stability-20260922/browser-release-plan.json` and current
`tools/riscv/lmbench_smoke.py` from its working directory.
Create a fresh output subdirectory, then run:

```sh
PYTHONPATH=. ASTERINAS_TEST_KERNEL=/path/to/kernel.Image \
  python3 /root/asterinas/target/lmbench-basic-20260923/run.py lmbench qemu-replay
```

A matching `kernel.elf` beside the Image enables failure PC symbolization.
The wrapper retains serial output and attempts CPU/IRQ capture on failure,
then tears down QEMU.
It has a 120-second experiment bound, separate VM startup limits,
and a 100-second outer limit around the smoke runner.
Those limits are failure containment, not workload durations.
No physical board was accessed and no test QEMU was left running.

## Follow-up order

The prior desktop-stop fix and its short regression are already on remote main;
[the stability evidence](2026-09-23-cgroup-events-and-media-reset.md)
remains the reference for that issue.
The original simultaneous physical playback/UART failure remains unresolved.

For LMBench, this 18-case smoke is the first working baseline.
Next, map the external task's exact version and acceptance commands onto this
baseline; add only missing required cases with bounded runs.
A later performance evaluation needs its own hardware, sampling and comparison
protocol rather than treating these smoke numbers as final scores.
