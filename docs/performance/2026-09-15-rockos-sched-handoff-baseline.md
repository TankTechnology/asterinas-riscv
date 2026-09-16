# RockOS native RISC-V scheduler-handoff reference

This is a Linux reference on the same Megrez board, **not** an Asterinas
measurement or evidence that Firefox's delay is a scheduler bug.

## Provenance and method

- Board: Milk-V Megrez, `eswin,eic770x`, four online CPUs `0-3`, up to 1.8 GHz.
- OS: RockOS Linux `6.6.87`, `riscv64`; board was already booted and reached over SSH at `10.100.19.200`. No reset, package install, or rootfs write.
- Source: `tools/benchmarks/sched_handoff/sched_handoff.c` at repository HEAD `634a3b536615591d78bbe90fc8e7e296532a233e`, SHA-256 `18a7c36811c806d04412ac754f06d48f3ed20c9de84200c9dc0719d8f6e92375`.
- Native build: board GCC `14.2.0-19rockos1`, `-O2 -pthread -Wall -Wextra -Werror`; dynamically linked against board `libc.so.6`. Binary SHA-256 `a6ff9574aab9aa6897a608de16f8cb54ce471d1ce6362a42cdcc4269675c4721`.
- Workload: 10,000 two-thread round trips per run, `SCHED_OTHER`, exact affinity readback, three repeat runs of each placement and mode. The source has a 20-second alarm and pre-faulted sample storage. Runs were sequential, not concurrent.
- RockOS reported about 0.26 one-minute load before the run. Its wall clock differed from the host, so only `CLOCK_MONOTONIC` durations are used.

## Results

All runs exited 0 with 10,000 completed samples and the requested affinity.
Times are **round-trip** nanoseconds, including synchronization and clock reads.

| Mode / CPU A→B | Run | p50 | p95 | p99 | elapsed |
|---|---:|---:|---:|---:|---:|
| blocking / 0→0 | 1 | 9,000 | 9,000 | 10,000 | 94,905,000 |
| blocking / 0→0 | 2 | 9,000 | 10,000 | 10,000 | 100,845,000 |
| blocking / 0→0 | 3 | 9,000 | 10,000 | 18,000 | 93,240,000 |
| blocking / 0→1 | 1 | 15,000 | 16,000 | 21,000 | 159,024,000 |
| blocking / 0→1 | 2 | 15,000 | 16,000 | 21,000 | 159,324,000 |
| blocking / 0→1 | 3 | 15,000 | 16,000 | 22,000 | 159,127,000 |
| yield / 0→0 | 1 | 4,000 | 6,000 | 6,000 | 43,150,000 |
| yield / 0→0 | 2 | 3,000 | 5,000 | 5,000 | 41,783,000 |
| yield / 0→0 | 3 | 4,000 | 5,000 | 6,000 | 41,586,000 |

Single-run maxima ranged much higher than p99, up to 5.159 ms; they may reflect
RockOS background activity or DVFS and cannot be attributed from this probe.
The current binary remains under RockOS `/tmp/asterinas-sched-handoff-20260915`
with its source under the same prefix. They are test artifacts, not installed
system tools.

## Use in Asterinas comparisons

Run the **same source/binary configuration** and CPU placements on Asterinas
only after its procfs CPU ledger and local browser timing workload are admitted.
Use multiple repeat runs in one boot and record CPU frequency/load and logging
settings. A difference identifies a candidate scheduling/futex cost; it does
not by itself explain pointer, keyboard, or public-page latency. Rust `make test`
cannot currently run natively on RockOS because `rustc` and Cargo are absent;
the board is immediately useful for native C tests and hardware/Linux controls.
QEMU on RockOS has no `/dev/kvm`, so TCG kernel tests are likely slower than the
persistent x86 development environment and are not the default build path.
