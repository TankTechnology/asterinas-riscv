# Logging and scheduling attribution results

## Scope

This change adds reproducible probes, not a scheduler or Firefox performance fix.
The board remained in RockOS throughout this work.
No boot defaults, Docker images, toolchains, or signal-test deadlines changed.

The logger probe has stack-owned observers and no shared measurement protocol.
Normal output uses a specialized no-op observer;
independent inspection of the final RISC-V executable confirmed that
`Printer<Unmeasured>::write_str` contains no attempted-byte counter updates.
Existing console locking and readiness callbacks are unchanged.
No new formal concurrency proof is claimed for this change.

## Scheduler handoff experiment

Use `tools/benchmarks/sched_handoff/sched_handoff.c` with arguments
`MODE 0 CPU_B 100`, for `MODE=yield,blocking` and `CPU_B=0,1`.
Five repetitions of each condition produce 20 samples per environment.
Each program records 100 individual round trips,
verifies both singleton affinities and SCHED_OTHER policy,
and prints only after timing and joining.
The sample array is pre-touched before thread creation.
Startup/barrier/join/output are outside the timed interval.

| Environment | Mode | CPU B | Median total for 100 trips (ms) | Median of run p50s (us) |
| --- | --- | ---: | ---: | ---: |
| Asterinas, 4-hart QEMU | Yield | 0 | 1194.8553 | 12005.4 |
| Asterinas, 4-hart QEMU | Yield | 1 | 0.5437 | 4.8 |
| Asterinas, 4-hart QEMU | Blocking | 0 | 3.7935 | 34.4 |
| Asterinas, 4-hart QEMU | Blocking | 1 | 4.6494 | 44.5 |
| RockOS, physical board | Yield | 0 | 0.485 | 4.0 |
| RockOS, physical board | Yield | 1 | 0.203 | 1.0 |
| RockOS, physical board | Blocking | 0 | 0.826 | 8.0 |
| RockOS, physical board | Blocking | 1 | 1.594 | 15.0 |

All 40 programs completed 100 trips, with 40 matching begin/end pairs and exit 0.
The collected JSON was checked for affinity, policy, count, and ordered quantiles.
These are round-trip synchronization costs, not pure context-switch latency.
Do not compute an OS speedup ratio between physical Linux and emulated Asterinas.

### Attribution

The same-QEMU comparison isolates a severe same-CPU voluntary-yield delay.
`FairClassRq::update_current` accounts `Yield` like `Tick`,
so a fair peer does not itself cause immediate selection.
With four harts and two equal-weight runnable threads,
the current formula gives a 12 ms period and about 6 ms per thread:
`max(0.75 ms * 2, 6 ms) * floor(log2(1 + 4))`.
A two-switch round trip therefore naturally approaches the observed 12 ms.
Blocking waits bypass this time-slice condition;
distinct-CPU threads do not require a local switch to exchange their turn.

This explains the new pure-yield probe, not every old signal-wait slowdown.
The earlier signal probe also blocks in signal syscalls and has different scheduling behavior.
No fresh Firefox profile links its delays to yield yet.

### RockOS environment

Linux 6.6.87; four online CPUs; boot ID
`14362804-06a0-4b24-9536-e4e721757650`.
CPU 0 and 1 used `cpufreq-dt`, governor `performance`,
with current/max frequency both 1800000 kHz at the environment snapshot.
Frequency was observed, not changed or continuously traced.
Load averages at the snapshot were 0.05/0.10/0.09.
The binary was staged at
`/var/tmp/asterinas-perf-baseline.ZTTg6xKK/sched_handoff-8ca0dae8`.

## Logging experiment

Boot the final kernel with `asterinas.log_profile=1` and
`console=ttyS0 loglevel=error asterinas.klog_capture=info init=/init`.
The probe runs before userspace and alternates the console threshold
and instrumentation state, keeping record contents and volume fixed.
The RISC-V timebase was 10000000 Hz.
Durations below are medians over five batches of 32 records each.

| Console threshold | Measured | Whole batch (us) | Memory preparation/publication (us) | Lock wait (us) | Locked formatting/send (us) |
| --- | --- | ---: | ---: | ---: | ---: |
| Error | No | 47.8 | Unmeasured | Unmeasured | Unmeasured |
| Error | Yes | 38.9 | 34.5 | No console calls | No console calls |
| Info | No | 6370.7 | Unmeasured | Unmeasured | Unmeasured |
| Info | Yes | 5688.6 | 40.9 | 5.3 | 5514.9 |

All 20 batches passed the profile validator, with expected counts and no invalid durations.
Synchronous locked formatting/send dominates visible-record cost in this experiment.
That interval also includes UART readiness callbacks;
this does not separately attribute UART polling, host emulation I/O, and callbacks.
The memory measurement uses user-record preparation, not kernel `format_args!` preparation.
Attempted byte counts are not confirmed UART delivery counts.

Measured batches being faster than unmeasured batches is not a negative instrumentation cost.
Short QEMU batches have warmup/order/host noise despite alternating conditions;
the back-to-back clock baseline and both raw modes are retained,
but this sample set does not establish a reliable perturbation estimate.
No physical-board log-profile result is available yet.

## Verification and provenance

- Native handoff integration: 11 tests passed in 40.119 s.
  Two real full-pipe tests enforce the process watchdog, including shared stdout/stderr.
- Log store and accumulator: 14 existing plus 5 new tests passed.
- Profile parser and existing klog gate: 21 tests passed.
- Final RISC-V kernel: release build succeeded; 12 existing kernel warnings remain.
- Final x86-64 kernel: release build succeeded; warnings remain.
  x86 timing is deliberately unavailable and was not hardware-qualified.
- Final four-hart QEMU with profiling enabled:
  20 valid logging batches followed by all 17 existing signal qualification programs passing.
- Independent spec and normal code reviews completed; findings were fixed and rechecked.

TDD evidence included missing-implementation failures for the new tools,
a real blocked-stdout failure, a real combined-output failure,
and parser failures on two nested-panic messages before their fixes.
The first integration boot also exposed a wrong boolean parameter registration;
the parser rejected missing measurements, then the flag registration was corrected.

| Final artifact | SHA-256 |
| --- | --- |
| RISC-V kernel | `03c9af8443227dce76d22820b0f2db0c994271c1b7f1e24288e2af47707bd0c7` |
| RISC-V handoff binary | `8ca0dae89c463e570110e9a2ddb859d53d78bcc1a5f1e25be78ce2b299c835dc` |
| Handoff C source | `18a7c36811c806d04412ac754f06d48f3ed20c9de84200c9dc0719d8f6e92375` |
| Handoff initramfs | `e96112dec803ffcb8b418fbe3c766283ce5d02d99c38f99893754232ba447cd2` |

Local raw evidence lives under `target/signal-group-stop/`:

- `handoff-qemu-final/{serial.log,result.json,initramfs.cpio}`
- `handoff-rockos-final.log`, `handoff-rockos-environment.log`
- `log-profile-final-qualification.log`, `log-profile-final-qualification.json`
- `handoff-native-final-tests.log`, `log-profile-final-python-tests.log`
- `log-profile-final-build.log`, `log-profile-final-x86-build.log`

The QEMU result manifest contains the exact diskless/networkless command.
The qualification run uses `stop-qualified-riscv-green/initramfs.cpio`
with the same QEMU CPU/memory/hart settings, an added profile flag,
and a 60-second external timeout; QEMU exited 0.
Raw build artifacts are intentionally not committed.

## Next change, not implemented here

Proposed minimal follow-up: after existing runtime accounting,
let an explicit fair `Yield` select a queued peer without waiting for the slice to expire.
Keep empty-queue behavior, higher-priority selection, weights, tick thresholds,
CPU placement, and all current runqueue locks unchanged.
Add exact kernel tests, model the relevant selection/ownership invariants,
then rerun the identical handoff and signal suites before hardware qualification.
This proposal is awaiting the user's response to the concrete design question.
