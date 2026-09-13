# Fair voluntary-yield correction

## Result and boundary

The same-CPU handoff probe no longer waits for ordinary fair time-slice expiration.
The only production scheduling change is accepting `UpdateFlags::Yield`
alongside `Wait` after the existing accounting and empty-peer check.
Runtime, pending weight changes, minimum virtual runtime, tick thresholds,
priority ordering, locking, and pick-before-reenqueue remain unchanged.
The `Stop` lint annotation is conditional on non-test builds because the new test uses it.

This fixes the measured voluntary-yield delay, not Firefox as a whole.
No board reboot, boot-default update, Docker replacement, download,
network change, test-deadline extension, or remote push was performed.

## Deterministic and model checks

`fair_yield_handoff` supplies a small unexpired runtime rather than using wall-clock delays.
Before the fix it failed specifically at the queued-peer `Yield` assertion.
After the fix it passed, including empty-queue behavior, runtime charging,
pending nice updates, unchanged unexpired/expired tick behavior, and peer identity.
`fair_yield_preserves_class_priority` passed with isolated, unspawned tasks,
checking peer replacement, higher-class precedence, and preservation of both old fair tasks.

Both exact tests passed separately on four-hart RISC-V QEMU
and four-vCPU x86-64 QEMU: one passed, zero failed in each of the four final runs.
RISC-V QEMU exits 0 even on a caught test assertion failure,
so result markers, not exit status alone, establish the red/green result.
The x86 successful test exits were 33 through `isa-debug-exit`.

The cached TLC runner checked all 111 distinct states (128 generated)
of the corrected finite selection model.
Three negative controls each failed the intended invariant after 22 distinct states:
ignored yield, reenqueue before selection, and loss of the old runnable task.
The model assumes the existing local-runqueue critical section is atomic.
It does not prove the lock, weak memory, migration, repeated-yield fairness,
actual context switching, or a refinement of the complete Rust implementation.
See `tools/verification/fair_yield/README.md` for the mapping and limits.

## Same-binary differential experiment

Old and new kernels ran sequentially with the same frozen initramfs and static executable.
Neither primary measurement boot overlapped a build launched by this task.
Each boot executed five repetitions of four conditions,
with 100 timed round trips per invocation and no hot-loop output.
All 40 invocations had matching begin/end markers, exit 0,
confirmed singleton affinities, SCHED_OTHER policy, 100 completions/samples,
and ordered nonnegative quantiles.
All per-run JSON records are retained; individual unsummarized timings are not emitted by the probe.

| Mode | CPU A/B | Old median total, 100 trips (ms) | New median total (ms) | Old median run p50 (us) | New median run p50 (us) |
| --- | --- | ---: | ---: | ---: | ---: |
| Yield | 0/0 | 1194.7852 | 2.0984 | 12005.4 | 18.2 |
| Yield | 0/1 | 0.7526 | 0.5994 | 6.0 | 5.3 |
| Blocking | 0/0 | 3.7801 | 4.1325 | 34.5 | 38.1 |
| Blocking | 0/1 | 5.7627 | 5.1576 | 56.1 | 49.7 |

The roughly 12 ms two-switch yield delay disappears when immediate peer selection is enabled.
These are synchronization round trips, not raw context-switch latency.
The small changes in the other conditions are not established improvements or regressions:
there are only five repetitions, fixed old/new order, and uncontrolled host noise.
In particular, same-CPU blocking was about 9% slower in this pair;
do not describe all scheduling modes as faster or equivalent.
No emulator-to-board speedup or Firefox startup improvement is inferred.

A preliminary new-kernel run overlapped compilation and is retained separately
as `fair-yield-handoff-new.log`, not used in the table.
Both primary boots print the archive's existing failed `/proc` mount warning
because its mountpoint is absent; the probe does not read `/proc`
and independently verifies placement through affinity syscalls.
The identical archive was preserved for the controlled comparison.

## Qualification and review

- The unchanged 17-program signal qualification archive passed every program with exit 0.
- The same boot produced all 20 expected logging batches with valid counts/durations;
  `tools/riscv/diagnostics/log_profile.py` accepted the transcript and runner exit 0.
- RISC-V and x86-64 release builds succeeded offline in the persistent container.
  Existing compiler warnings remain; this is not a full lint-clean claim.
- Rust formatting checks and `git diff --check` passed.
- Independent specification and ordinary code reviews found no blocking code/model defects.

Launcher limitations were not treated as successful tests:
OSDK's default QEMU launch lacked unrelated `ext2.img` fixtures,
so generated test binaries were frozen and booted disklessly.
A prefix filter selected zero tests; the exact names above were subsequently used.
The x86 microvm UEFI attempt did not boot the multiboot image and timed out;
final x86 tests used Q35/SeaBIOS and `earlycon` to retain exact test output.

## Reproduction and provenance

Source baseline: `7c0c5d687`.
Artifact paths below are relative to the worktree root.

| Artifact | SHA-256 |
| --- | --- |
| Old RISC-V kernel | `03c9af8443227dce76d22820b0f2db0c994271c1b7f1e24288e2af47707bd0c7` |
| New RISC-V kernel | `38277fad7633ef98c5b96324d6b3a7d0a5ad824bc90a7d53bcbfc222b5ac896e` |
| Handoff executable | `8ca0dae89c463e570110e9a2ddb859d53d78bcc1a5f1e25be78ce2b299c835dc` |
| Handoff initramfs | `e96112dec803ffcb8b418fbe3c766283ce5d02d99c38f99893754232ba447cd2` |
| Signal qualification initramfs | `a848faa26f60c08b123409ddd7df099013c78fa77969cfdc23b7f2b2ce0033ef` |

Run inside the persistent development container, from `/root/asterinas`:

```sh
timeout --kill-after=3s 120s qemu-system-riscv64 \
  -machine virt \
  -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
  -m 2G -smp 4 -nographic -nic none -no-reboot \
  -kernel target/signal-group-stop/fair-yield-release-kernel.Image \
  -initrd target/signal-group-stop/handoff-qemu-final/initramfs.cpio \
  -append 'console=ttyS0 loglevel=error asterinas.klog_capture=info init=/init'
```

For the old run substitute `log-profile-final-kernel.Image`.
For qualification use `stop-qualified-riscv-green/initramfs.cpio`,
add `asterinas.log_profile=1`, and use the existing 60-second external bound.
For the exact RISC-V ktests use the frozen test images without an initrd.
For x86 use the frozen `.elf` images with `-machine q35`,
`-cpu Icelake-Server,+x2apic`, and `-device isa-debug-exit,iobase=0xf4,iosize=0x04`;
append `earlycon console=ttyS0 loglevel=error`.

Raw evidence under `target/signal-group-stop/`:

- `fair-yield-red-ktest-qemu.log`, `fair-yield-green-ktest-qemu.log`
- `fair-yield-priority-ktest-qemu.log`
- `fair-yield-x86-{handoff,priority}-ktest-final.log`
- `fair-yield-handoff-{old,new}-idle.log`, `fair-yield-handoff-comparison.json`
- `fair-yield-signal-qualification.log`, `fair-yield-log-profile.json`
- `fair-yield-release-build.log`, `fair-yield-x86-release-build.log`

TLC evidence is in `target/fair-yield-model/run.TwJ8uids/`.
Raw binaries/logs are local, not committed.

## Next qualification

Use the small probe on physical Asterinas before any full desktop experiment.
Then measure Firefox startup, first usable frame, and input latency separately,
with memory log capture retained and console verbosity controlled.
Detailed-console timing instability remains a separate logging-path problem;
this scheduler change neither makes UART output asynchronous nor establishes its hardware cost.
