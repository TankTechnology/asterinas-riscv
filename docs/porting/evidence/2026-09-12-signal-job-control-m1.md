# STOP/CONT cancellation: implementation and model-checking evidence

## Outcome

M1 repairs two independently reproduced signal-ordering defects:

- Opposing STOP/CONT signals remained pending in shared or private queues.
- A STOP already selected for delivery could still stop the process after a
  later CONT had continued it.

The corrected finite protocols pass TLC; the actual Rust implementation passes
focused kernel tests and four-hart RISC-V QEMU syscall regressions. This is not
a proof of the entire kernel, and it does not establish that Firefox startup
or the approximately 107-second normal-console physical recovery is fixed.
No board, serial session, default boot menu, or installed kernel was changed.
No remote push was performed.

The implementation follows the previously approved
[Linux/OSTEP design](../../superpowers/specs/2026-09-12-signal-job-control-design.md)
and the scoped [M1 plan](../../superpowers/plans/2026-09-12-signal-job-control-m1.md).
The source references and division between generation, delivery, and wait
predicates are recorded there; Linux internal structures are not copied into
the safe-Rust kernel wholesale.

## Actual synchronization contract

Each process owns a signal coordinator; each thread owns one revocable
selected-stop marker. Queue storage and process stop/wait status are retained.

| Operation | Protected work |
| --- | --- |
| Generate STOP/CONT | Stabilize task membership, then take the coordinator. Clear opposing signals from the shared queue and every private queue; CONT also revokes selections and resumes the process. Enqueue before releasing coordination. |
| Select for delivery | Hold dispositions before coordination; dequeue and prepare the selection before unlocking. Every delivered signal gets eligibility because ptrace can substitute STOP. |
| Commit STOP | Under coordination, consume eligibility, check committed exit and current/shared pending SIGKILL, then update the real process stop status. |
| Commit exit | Mark terminal state at actual group exit or last-thread exit. Merely generating SIGKILL is not an eager group-exit commitment, especially for exec sibling termination. |
| Raw consumption | signalfd and sigtimedwait dequeue under coordination without selecting or executing a stop action. |

The coordinator never nests task membership, disposition acquisition, or ptrace
state acquisition. Queue/selected-stop locks are below it. Observer callbacks
and wakes occur after releasing it. Guards acquired in generation are released
before callbacks; existing exit/exec callers can still hold task membership
while sending sibling SIGKILL. That existing caller contract is not claimed to
have been redesigned.

CONT's default delivery action is now empty: a delayed old CONT must not undo
a later STOP. Blocked ptrace substitution is still requeued through its original
queue and generation path, retaining the Linux-compatible behavior described
in the design. The existing durable waiter token is reused, not replaced with
a new wait primitive.

## Formal checks and negative controls

Run from the repository root:

```sh
bash tools/verification/signal_job_control/run.sh
```

The [model README](../../../tools/verification/signal_job_control/README.md)
specifies each operation, bounds, fairness assumptions, artifact checksums,
and omitted behaviors. TLC v1.7.4 uses one worker and a 768 MiB heap. Only its
2.2 MiB official JAR is downloaded on an empty cache; it is checksum-pinned and
reused. Kernel builds reuse the existing Docker and Cargo caches offline.

Final repeated artifacts: `target/signal-job-control-model/run.XQNyCFH7/`.

| Configuration | Distinct states | Result |
| --- | ---: | --- |
| Correct job-control protocol | 7,407 | Exhaustive safety pass, zero queued states |
| Correct wait/wake protocol | 194 | Safety and fair liveness pass |
| Missing pending cancellation | 13 | Expected pending-history violation |
| Missing selected-stop revocation | 532 | Expected stale-stop commitment |
| Missing remembered wake | 60 | Expected lost-wake violation |
| Missing remembered wake, liveness | 169 | Expected infinite-wait counterexample |

The cancellation history oracle does not grant implementation permissions;
negative controls therefore do not silently repair themselves. The job-control
model bounds generation to three events and two threads. The wait model covers
one stop/resume episode with two waiters and explicit weak fairness.

Atomicity is an implementation contract, not a TLC result about Rust locks.
The models omit ptrace substitution/requeue, masks/dispositions, raw consumers,
SIGKILL dequeue, and stopped-thread delivery. They do not prove Rust or RISC-V
memory ordering, whole-kernel deadlock freedom, or full group-stop semantics.
Separate ordinary specification, model-quality, implementation, and test reviews
checked these boundaries. The retired code-review skill was not used.

## Actual-code regressions

All experiment paths below are relative to `target/signal-job-control/` unless
otherwise specified. Logs and generated images are ignored local artifacts.

1. `stop_continue_pending.c`: 48 cases cover TSTP/TTIN/TTOU, both opposing
   orders, all kill/tgkill route pairs, and ignored/default dispositions while
   blocked. They check unrelated pending preservation and synchronous
   sigtimedwait consumption. Six further pipe-synchronized sibling cases
   check that a signal sent to one thread clears an opposing signal queued
   privately to another. Native Linux passes 54/54; the frozen old kernel
   fails 54/54 (`pending-final-red/`); the final kernel passes 54/54.
2. `ptrace_continue_cancels_selected_stop` holds a dequeued SIGSTOP in a ptrace
   delivery stop, generates blocked SIGCONT, then resumes with the old STOP.
   Linux and the corrected kernel exit normally. The old kernel fails the
   bounded completion assertion; SIGKILL cleanup and reaping then succeed
   (`selected-stop-red-bounded/`). This fixes the interleaving without production
   sleep/yield instrumentation or probabilistic browser experiments.
3. Three exact kernel tests pass, one executed test per invocation in
   `ktest-final.log`: selected -> CONT -> commit (plus fresh selection),
   exit/pending-KILL precedence, and queue discard/coalescing/RT-count integrity.
   The cancellation-disabled Rust fixture emitted `stale_commit=true` before
   its failing assertion; the corrected test emits `false` and passes. This
   fixture was never committed. Its assertion-unwind run timed out, so that
   timeout alone is not counted as a successful test-runner result.
4. Final four-hart QEMU run `regressions-cleanup-reviewed/` passes all eight
   programs: pending cancellation, existing eight-case stopped-CONT, signalfd,
   ptrace (including the new case), kill, signal_test2, job_control, and rseq.
   Each has an explicit zero guest result marker. QEMU exits zero; elapsed
   time is 10.357 seconds for this test bundle, not a browser benchmark.
5. `boot-final/` passes the unchanged Stage1 Basic shell command round trip and
   automatic Probe/reboot. No disks or network devices are attached.
6. Formatting, whitespace, and RISC-V `clippy --ktests` complete successfully.
   Existing warnings remain; this is not a warning-free full CI claim.
   The 120 selected boot-menu, debug-console, probe, and debugging-tool host
   regressions also pass (`host-regressions.log`).

The pending regression now bounds each child wait and kills/reaps failed or
stopped children; a timeout aborts the rest of the matrix. `cleanup-check.log`
tests its actual helper with both a stopped and a permanently waiting child,
then verifies ECHILD after each (2/2). Ptrace's new case similarly polls with a
bound and kills/reaps a stuck tracee. An outer QEMU timeout still bounds failures
where the kernel itself cannot complete SIGKILL or wait; userspace cannot
guarantee cleanup in that situation.

## Reproduction and artifact identity

The C regressions are registered in the normal process regression suite.
The new pending program's Makefile target includes `-pthread`. Native examples:

```sh
gcc -O2 -pthread -Wall -Wextra -Werror \
  test/initramfs/src/regression/process/signal/stop_continue_pending.c \
  -o /tmp/stop-continue-pending
/tmp/stop-continue-pending
```

Build with the persistent launcher and cached repository OSDK as in the M1
plan. For focused kernel tests, use the **exact function name**, not
`process::signal`: the current OSDK whitelist is suffix-based and that module
string executed zero tests. The default test scheme also expects external
filesystem images. This run used an ignored diskless QEMU adapter with the
existing `--qemu-exe` option, not fake filesystem images or a fresh container:

```sh
cd kernel
../osdk/target/debug/cargo-osdk osdk test continue_cancels_selected_stop \
  --target-arch riscv64 --scheme riscv --features riscv_sv39_mode \
  --initramfs ../target/shutdown-latency/stage1/initramfs.cpio \
  --qemu-exe /root/asterinas/target/signal-job-control/qemu-ktest.sh
```

The adapter runs `qemu-system-riscv64 -machine virt -m 2G -smp 4 -nographic
-nic none -no-reboot`, CPU
`rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true`, the OSDK kernel and
initramfs, and `console=ttyS0 loglevel=error`, with a 25-second bound. No missing
disk launch, zero-test invocation, or timeout is counted as a pass.

The first combined ptrace bundle lacked `/bin/true`; exec tests failed with
ENOENT on both old and new kernels. Adding the standard BusyBox applet link
to the test archive made the complete suite pass. No kernel workaround was
added for this fixture error.

| Artifact | SHA-256 |
| --- | --- |
| Old kernel, `../shutdown-latency/kernel.Image` | `5da7586448d45ed5c8b65e1b3a34ebd87a45de54d0df19dd51ab42fa50bc4457` |
| Final kernel, `kernel-final.Image` | `457261fd51c1ca4829d0df9b5c263832fbf0b4dfed12360196f2dd88409351dc` |
| Unchanged Stage1, `../shutdown-latency/stage1/initramfs.cpio` | `9ef253dadad3f399e6152ad8681319725e99562081f237ef851a7ec11c3ba405` |
| Final eight-program regression archive | `d99bc776fdc964e3af6777c6f50a87971fe4e13437a528dd0bd6c49714d021f2` |

## Remaining milestones

M2 must address stopped-thread delivery filtering and full group-stop
participation/reporting, including lifecycle races and broader ptrace injection
cases. M3 must qualify the final normal-console physical recovery and browser
behavior. M1's signal fixes do not by themselves identify the complete cause
of the previously measured shutdown delay or establish a Firefox speedup.
