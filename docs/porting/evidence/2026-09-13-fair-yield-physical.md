# Fair-yield physical qualification and remaining latency

## Outcome

Four bounded Megrez probe boots completed and returned to fresh U-Boot prompts.
The new kernel removes the usual time-slice-sized voluntary-yield delay,
but physical tail latency is not qualified as stable.
A separate CPU-affinity inheritance defect was reproduced on physical Asterinas
and QEMU, using a small executable that passes on physical RockOS.
No Firefox boot was attempted after these findings.

The tested new kernel is the unchanged release artifact of `138cca9d1`.
This experiment does not modify kernel code, scheduling policy, clock settings,
networking, Debian partition 2, the installed boot menu, or the default kernel.
The previous finite selection model does not cover the newly observed latency
or CPU-affinity inheritance behavior.

## Controlled handoff runs

Run order was new, old, new, then an early-main-affinity control with the new kernel.
Each boot runs five repetitions of `yield`/`blocking` with CPU A=0 and CPU B=0/1,
100 round trips per invocation, using the same static handoff executable.
All 80 handoff invocations returned zero with matching begin/end markers,
correct completion/sample counts, verified singleton final affinities,
and ordered nonnegative quantiles.
This is functional completion, not a latency pass.

| Boot | Same-CPU yield: median total for 100 trips (ms) | Median of run p50s (us) | Worst same-CPU yield trip (ms) | Worst trip across all modes (ms) |
| --- | ---: | ---: | ---: | ---: |
| New 1 | 329.986 | 25 | 1226.661 | 1226.661 |
| Old 1 | 2918.924 | 12023 | 12239.182 | 12239.182 |
| New 2 | 5.200 | 27 | 0.406 | 100.461 |
| New, early-main-affinity control | 5.041 | 27 | 0.378 | 100.155 |

The first new boot's largest same-CPU yield samples in repetitions 1–4 were
149.012, 324.675, 683.755, and 1226.661 ms.
Most other samples in those same invocations remained in the microsecond range.
The second new boot did not reproduce that increasing same-CPU tail,
but contained a 100.461 ms cross-CPU yield sample.
The control boot contained a 100.155 ms cross-CPU blocking sample.
None of these samples was discarded or converted into a timeout success.

The old kernel also has long tails, so their existence is not new to the yield fix.
This small, fixed-order experiment does not establish that the fix cannot
affect tail probability, nor that it improves every workload.
For example, median run p50 for cross-CPU yield was 12 us on the old boot
versus 39 and 52 us on the two new boots.
Blocking results also varied substantially between boots and modes.
All per-run results remain in `all-handoff-validated.json`.
Do not infer an overall scheduler, Linux/Asterinas, or Firefox speedup.

The early-main-affinity control only prefixes the existing invocation with
`/bin/busybox taskset 1` before executing the benchmark.
The executable still sets and verifies both final affinities before timing.
The control is **not** evidence that both threads avoided initial migration:
the inheritance defect below allows a newly created responder to start elsewhere.
It also does not distinguish a placement effect from ordinary inter-boot variation,
because the preceding unmodified new boot already lacked the large same-CPU tail.

## Independently reproduced affinity defect

The standalone diagnostic chooses one CPU from the caller's allowed mask,
sets and reads back the parent's singleton affinity, then checks `fork()`
and default-attribute `pthread_create()` children without setting their affinity.
It compares complete masks, not just CPU counts, joins/reaps children,
and uses a 20-second alarm bound.

| Environment | Parent mask count | Fork child | Pthread child | Executable exit |
| --- | ---: | --- | --- | ---: |
| Native Linux in persistent development container | 1 | Exact inherited mask | Exact inherited mask | 0 |
| RockOS 6.6.87 on Megrez | 1 | Exact inherited mask | Exact inherited mask | 0 |
| Four-hart QEMU, new Asterinas | 1 | 4 CPUs, mismatch | 4 CPUs, mismatch | 1 |
| Megrez, new Asterinas | 1 | 4 CPUs, mismatch | 4 CPUs, mismatch | 1 |

The physical diagnostic ran after the control's 20 handoff invocations,
so it cannot explain their measured latency by executing concurrently with them.
Its `AFFINITY_EXIT=1` is a genuine failed conformance check,
not included in the 80 successful handoff invocations.

Code inspection identifies the direct implementation gap:
`kernel/src/process/posix_thread/builder.rs` constructs each POSIX thread with
`CpuSet::new_full()`, while the clone construction path supplies no inherited mask.
Linux documents inheritance across fork and preservation across exec in
the [affinity manual](https://man7.org/linux/man-pages/man2/sched_setaffinity.2.html).
The measured pthread result is also retained separately from that citation.

This defect weakens external affinity controls that rely on descendants inheriting them.
It does **not** invalidate the existing probe's verified final singleton masks,
and it has not been established as the cause of the long-tail samples.
Fixing inheritance and understanding cross-CPU virtual-runtime placement
are separate follow-up checks; no scheduler normalization fix is claimed here.

## Deployment and recovery

RockOS was initially reachable over SSH with boot ID
`14362804-06a0-4b24-9536-e4e721757650`.
The serial port was exclusively acquired after checking for another owner.
`/boot` had only about 5.2 MiB available, so no artifact was added there.

SCP staged new files in `/var/tmp/asterinas-fair-yield.PeOiTdcd`
on the existing RockOS partition 3, which had about 73 GiB available.
Remote SHA-256 checks matched the local frozen artifacts, followed by `sync`.
After a normal RockOS reboot, read-only `ext4ls mmc 1:3` confirmed accessibility.
The temporary selector was then loaded with one paced command:

```text
sysboot mmc 1:3 any 0x88200000 /var/tmp/asterinas-fair-yield.PeOiTdcd/experiment.conf
```

Choice 1 runs the new kernel, choice 2 the old kernel.
The separate `pinned.conf` uses the control archive.
Both use the already prepared framebuffer/USB DTB, avoiding repeated manual fixups.
They do not replace the persistent selector or change firmware variables.
Each probe uses `console=ttyS0 loglevel=error asterinas.klog_capture=info`,
the existing init script's explicit reboot, and `asterinas.reboot_after=90`.
The software deadline is not a hardware-watchdog guarantee.

The four probe operations, including menu commands, execution and firmware recovery,
took 22.97, 50.96, 21.36, and 20.71 seconds respectively.
These are host-observed operation durations, not kernel boot latency.
No electrical reset or power cycle was requested.

Final serial login and SSH confirmed RockOS 6.6.87, boot ID
`649b2ca3-dc67-4503-b85d-6342b361281d`.
The installed and vendor menu hashes remained respectively
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`
and `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.
No cleanup removed old artifacts; the new temporary files are retained for reproduction.
In the one-off runner's RockOS records, `completed=true` denotes successful login;
`recovered` is only set for the firmware-return phases, not the RockOS phase.

## Provenance and next step

All local raw evidence is under `target/fair-yield-board/`:

- `staging.log`, `affinity-linux.log`, `final-ssh.log`.
- `new-1/`, `old-1/`, `new-2/`, `pinned-1/`: serial logs and operation results.
- `all-handoff-validated.json`: all 80 validated per-run handoff summaries.
- `affinity-qemu.log`: QEMU handoff control and failed inheritance diagnostic.
- `affinity_inherit.c`, `affinity-inherit-riscv`: exact diagnostic source/binary.
- `experiment.conf`, `pinned.conf`, `board_run.py`: one-off test configuration/runner.

| Artifact | SHA-256 |
| --- | --- |
| New kernel | `38277fad7633ef98c5b96324d6b3a7d0a5ad824bc90a7d53bcbfc222b5ac896e` |
| Old kernel | `03c9af8443227dce76d22820b0f2db0c994271c1b7f1e24288e2af47707bd0c7` |
| Original handoff initramfs | `e96112dec803ffcb8b418fbe3c766283ce5d02d99c38f99893754232ba447cd2` |
| Control plus affinity diagnostic initramfs | `1ff9016eb1942b49774cbb88134e8501ad49cbe5a5a53e6df273b80bf642aeeb` |
| Handoff executable | `8ca0dae89c463e570110e9a2ddb859d53d78bcc1a5f1e25be78ce2b299c835dc` |
| Affinity diagnostic executable | `68f064ac200ab392513a7bf5406d9753a28398e26aded031c48774ae3019e5c0` |
| Affinity diagnostic source | `54f2c3934fd2aa5f3c2e412a6e42d8157f52650a00ec35749eb66a8e5a7b6da0` |
| Prepared DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` |

The original and control archives retain the existing missing `/proc` mountpoint warning;
these diagnostics query affinity through syscalls and do not depend on procfs.
No package downloads, OSDK installation, container recreation, or kernel rebuild was needed.
The small new diagnostic was compiled with the cached native and RISC-V compilers.

Next, promote the affinity reproducer into a maintained regression and repair
inheritance with a snapshot of the creating thread's allowed mask.
Then recheck latency with effective inherited placement and a bounded outlier recorder.
Firefox startup, first usable frame, and input-latency measurements remain deferred;
neither desktop performance nor verbose-console hardware cost passed this experiment.
