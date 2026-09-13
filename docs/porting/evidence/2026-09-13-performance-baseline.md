# Performance attribution baseline, 2026-09-13

These are diagnostic observations, not performance qualification.
Source baseline: `88053780a` on `codex/megrez-boot-main`.
No kernel source, Docker image, toolchain, or board boot defaults changed in these runs.

## Fresh tests

Every listed sample completed 10,000 signal handoffs without a reported failure.
Times below are the final per-program elapsed measurement, not boot-to-exit duration.

| Environment | Placement | Protocol | Elapsed (ms) | Maximum wait (ms) |
| --- | --- | --- | ---: | ---: |
| RockOS Linux 6.6.87 | CPU 0 | Yield | 122.922 | 0.150 |
| RockOS Linux 6.6.87 | CPU 0 | Blocking | 134.364 | 0.046 |
| RockOS Linux 6.6.87 | CPU 1 | Yield | 123.338 | 0.037 |
| RockOS Linux 6.6.87 | CPU 1 | Blocking | 135.473 | 0.043 |
| Asterinas QEMU | One hart, run 1 | Yield | 1645.4054 | 4.1263 |
| Asterinas QEMU | One hart, run 2 | Yield | 1488.2620 | 3.2824 |
| Asterinas QEMU | One hart, run 3 | Yield | 1117.1480 | 3.3050 |

The RockOS conditions each have only one sample.
These existing binaries print progress every 1,000 iterations;
they are not the proposed quiet-hot-loop performance benchmark.
Do not compute a Linux/Asterinas speed ratio across hardware and emulation.
Single-hart QEMU did not reproduce the earlier physical 15-second yield-test timeout.
This rules out only the claim that single-CPU execution inevitably reproduces it.

## Artifact identities

| Artifact under `target/signal-group-stop/` | SHA-256 |
| --- | --- |
| `stop-state-kernel.Image` | `dc25f4ec117846a1fa3f71b6dbb16755eefef550a033192f0f9cc5f6dee54f64` |
| `sigwait-timing-v2-qemu/initramfs.cpio` | `b4ebc255aa43a7ab876d057264299b04cc801dac330b43c878377e985f9c4f9e` |
| `sigwait-timing-v2` | `efa8e7a79abe1d915cf6c09b6dcab33464a51123098f159cb07697a3ace2b35f` |
| `sigwait-blocking-final` | `770fbc7300a42b1676b091f9531e5ae8c7486e0b3d19fec33380549e2490d0bf` |

Raw logs are local build artifacts, not committed files:

- `target/signal-group-stop/perf-rockos-pinned-baseline.log`
- `target/signal-group-stop/perf-yield-smp1-baseline.log`

## Commands and completion evidence

RockOS boot ID: `14362804-06a0-4b24-9536-e4e721757650`.
Two existing binaries were copied into a fresh directory,
`/var/tmp/asterinas-perf-baseline.ZTTg6xKK`, without touching `/boot`.
The board remains running RockOS; there was no Asterinas physical boot in these tests.

Run on RockOS from that directory:

```sh
for cpu in 0 1; do
    for binary in sigwait-timing-v2 sigwait-blocking-final; do
        taskset -c "$cpu" ./"$binary"
        printf 'PINNED_BASELINE cpu=%s binary=%s rc=%s\n' "$cpu" "$binary" "$?"
    done
done
```

All four `PINNED_BASELINE` markers report `rc=0`,
each preceded by `SIGWAIT_RACE passed=10000 failed=0`.
Affinity was requested using Linux `taskset`; these old binaries do not print affinity readback.
The loop's shell exit alone is not a test oracle.

QEMU command from the worktree on the development host:

```sh
docker exec -w /root/asterinas asterinas-dev-v1-4f054ba7e4d3-b058e7f2c917 \
    timeout --kill-after=3s 55s qemu-system-riscv64 \
    -machine virt \
    -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
    -m 2G -smp 1 -nographic -nic none -no-reboot \
    -kernel target/signal-group-stop/stop-state-kernel.Image \
    -initrd target/signal-group-stop/sigwait-timing-v2-qemu/initramfs.cpio \
    -append 'console=ttyS0 loglevel=error init=/init'
```

The command exited 0.
The archive executes three copies of the yield workload in order;
`REGRESSION_sigwait-timing-v2`, `REGRESSION_sigwait-timing-v2-2`,
and `REGRESSION_sigwait-timing-v2-3` all report 0,
each with a 10,000-pass summary.
This invocation did not explicitly set `asterinas.klog_capture`;
it is not a controlled console-only comparison with earlier info-capture physical runs.

## Remaining questions

The earlier physical verbose/quiet difference remains unresolved.
The console's IRQ-disabled synchronous-send path is confirmed by inspection,
but neither its measured cost nor a complete causal link to Firefox is established.
Fair yield decisions, wakeup preemption, and CPU placement remain separate hypotheses.
There are no fresh Firefox timings or new formal-verification results in this baseline.
