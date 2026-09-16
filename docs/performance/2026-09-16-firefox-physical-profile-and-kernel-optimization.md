# Firefox physical profile and first kernel optimization, 2026-09-16

This record separates physical measurements, perturbing diagnostic evidence,
and QEMU controls.  It does not treat a synthetic browser event as physical
keyboard-to-HDMI latency, add blocking syscall wall time across threads, or
infer a kernel CPU hotspot from one instrumented boot.

## Physical workload and provenance

The accepted physical capture is under
`target/current-main-physical-graphics/physical/firefox-profile-baseline-20260916-v9`.
It used the deployed kernel SHA-256
`aa9af41565aa4486272c84be10650843146457b045dc0f7b8f91fa974f53babb`,
deployed Stage1 SHA-256
`dcc400fe92fdbef01a0a92443bd3e8b18eb74563d7c2d8b39e1ec9120a91735e`,
four RISC-V harts, the local immutable browser fixture, Firefox PID 127, and
Xorg PID 112.  The CPU ledger contains twenty approximately 0.504-second
intervals (10.076 seconds total).  The thread ledger covers 12.513 seconds.

| Measurement | Result | Boundary |
|---|---:|---|
| Firefox CPU | 14.130 s user + 4.370 s kernel | 1.836 cores averaged over the CPU-ledger window |
| Firefox kernel share | 23.62% | CPU time, not blocked syscall wall time |
| Xorg CPU | 0.370 s | 0.0367 cores; Xorg was not the dominant CPU consumer |
| Context switches | 23,845 | 2,366.6/s |
| Runnable tasks | 3.05 mean | sampled `/proc/stat` endpoints |
| CPU 0/1/2/3 busy | 15.37% / 98.21% / 19.85% / 99.32% | mean of per-interval busy fractions |
| Firefox main thread | 7.560 s | sampled on CPU 3 |
| DOM Worker | 3.010 s | sampled on CPU 1 |
| Renderer | 0.510 s | 0.130 s user + 0.380 s kernel |

No sampled thread changed its last-CPU value during the thread intervals.  A
migration between interval endpoints remains possible.  The two hottest
Firefox threads were already observed on different busy CPUs, so this capture
does not justify pinning Firefox or changing scheduler affinity policy.

The browser-side synthetic event-to-`requestAnimationFrame` checkpoint was
saved before strict Navigation Timing validation.  Median first-rAF latency
was 23 ms for keyboard, 20 ms for pointer, and 23 ms for scroll; p95 was 64,
76, and 123 ms respectively.  Median next-rAF latency was 31 ms for all three;
p95 was 72, 95, and 140 ms.  These figures expose browser event-loop tail
latency, but do not include USB input, Xorg delivery, framebuffer scanout, or
HDMI display latency.  Firefox again returned a negative `fetchStart`; the
protocol correctly rejected the navigation record without clamping it while
retaining the independently valid interaction checkpoint.

## Diagnostic attribution

A release-kernel QEMU boot with syscall, futex, VM, page-cache, and read-detail
diagnostics reached Marionette in 44.178 seconds, while an adjacent
non-instrumented control reached it in 32.512 seconds.  The roughly 36%
measurement overhead means this run is attribution evidence only.

At its latest Firefox PID 157 snapshots, the diagnostic run recorded 31,504
page faults in 4,414 jiffies, including 1,398 executable faults in 1,863
jiffies, and 22,528 page-cache reads in 3,265 jiffies.  The syscall ledger
recorded 2,330 futex calls with 191,459 elapsed jiffies and 1,115 `ppoll` calls
with 21,433 elapsed jiffies.  These elapsed counters include blocking wall
time from overlapping Firefox threads; they cannot be summed or interpreted
as CPU time.  A futex detail record named `spurious` only means the waiter was
resumed without removing its futex item through the normal wake path; signals
and external wakes are not separated by the current probe.  It is not proof
of a futex correctness defect.

Executable faults invoke RISC-V instruction-cache synchronization.  To test
whether its all-hart RFENCE dominated startup, six low-diagnostic QEMU boots
alternated the correct global mode with an explicitly unsafe, diagnostic-only
local-hart mode.  Structured records are under
`target/physical-firefox-validation/qemu-{global,local}-icache-structured-r[1-3]-20260916`.

| Mode | Total host seconds, three runs | Median total | Median Firefox exec to Marionette |
|---|---|---:|---:|
| Correct global synchronization | 32.051, 37.497, 34.684 | 34.684 s | 8.927 s |
| Local-hart diagnostic | 34.979, 38.003, 35.171 | 35.171 s | 8.810 s |

The launch-phase median differs by about 1.3% and individual runs overlap.
This experiment does not identify global RFENCE as the primary Firefox
bottleneck.  Local-only synchronization remains unsuitable for SMP because a
task can migrate and a physical frame can later execute on another hart.

## Confirmed hot-path defect and optimization

The physical info-level serial stream emitted 142 warning records in a
representative 10-second Firefox/network window.  They included 84 ignored
`POSIX_FADV_SEQUENTIAL` warnings, 42 generic unimplemented-syscall warnings
(the workload used RISC-V syscall 272, `kcmp`), 12 existing `SCM_RIGHTS`
resource-leak warnings, and four other warnings.  The first two classes are
ordinary syscall-path compatibility diagnostics, not exceptional kernel
failures.  Formatting, locking, and sending them on the UART competes with the
interactive workload and obscures actionable warnings.

The kernel now emits ignored `posix_fadvise` modes and generic unknown syscall
numbers at debug level.  The `SCM_RIGHTS` warning remains visible because it
reports a real resource-lifetime limitation.  This removes 126 of the 142
observed warning records (88.7%) from a normal info-level run without changing
syscall results or hiding the specific resource warning.

The regression first failed against the old behavior with 128 passing and two
failing checks.  Against the change, the focused kernel-mode syslog suite
passed all 130 checks and established that 64 sequential-advice calls and 64
unknown syscalls do not increase the warning-level capture.

An old/new quiet-log QEMU comparison is intentionally not reported as a
speedup: quiet mode already filters these warnings, three-run medians were
noisy, and code layout changed.  The optimization's performance claim is
limited to eliminating the verified info-level physical warning storm.

## Missing graphics compatibility syscall

The warning census also exposed syscall 272, `kcmp(2)`, as a real compatibility
gap rather than a logging-only issue.  Mesa/Gallium uses `KCMP_FILE` to ask
whether two descriptors refer to the same open file description.  Returning
`ENOSYS` forces graphics userspace away from that Linux interface, even though
it was not shown to be the main CPU bottleneck in this workload.

The kernel now implements the two resource comparisons required by this path:

- `KCMP_FILE`, comparing open file-description identity;
- `KCMP_FILES`, comparing file-table identity, including `CLONE_FILES`.

The implementation checks read access with real credentials against both
target processes, returns `ESRCH` and `EBADF` for invalid targets and file
descriptors, and explicitly returns `EOPNOTSUPP` for Linux comparison classes
not implemented by this initial subset.  Unequal supported resources return
3, the Linux result for inequality without address-order information.  It is
wired as syscall 272 on
RISC-V/generic architectures and 312 on x86-64.  The pointer-identity primitive
has a kernel-mode unit test, and the syscall regression covers duplicated and
separately opened descriptors, forked file tables, `CLONE_FILES`, error paths,
and 1,000 self-comparisons to catch accidental same-table lock recursion.

The focused syscall test passed all 41 assertions on both RISC-V QEMU and
x86-64 QEMU.  The read-only-arc identity kernel test passed on RISC-V.  On the
physical board, `kcmp(getpid(), getpid(), KCMP_FILES, 0, 0)` returned zero with
`errno` zero before the final Firefox workload.  This removes the observed
graphics-stack fallback, but no speedup is attributed to it without a separate
A/B experiment.

## Physical after captures

Two independent captures with the quieted syscall diagnostics completed on the
physical board.  The second also contained the new `kcmp` implementation.  All
captures used the same local fixture, four harts, and approximately ten-second
CPU ledger; the exact artifacts are under
`target/physical-firefox-validation/physical-log-suppression-after-20260916`
and `target/physical-firefox-validation/final-kcmp-physical-20260916`.

| Measurement | Original baseline | Quiet diagnostics | Quiet diagnostics + `kcmp` |
|---|---:|---:|---:|
| Ledger duration | 10.076 s | 10.118 s | 10.091 s |
| Firefox CPU | 18.500 CPU-s | 9.940 CPU-s | 10.240 CPU-s |
| Mean Firefox cores | 1.836 | 0.982 | 1.015 |
| Firefox kernel share | 23.62% | 35.92% | 35.35% |
| Xorg CPU | 0.370 CPU-s | 0.410 CPU-s | 0.400 CPU-s |
| Context switches | 2,366.6/s | 2,204.9/s | 2,143.9/s |
| Mean runnable tasks | 3.05 | 2.20 | 2.00 |
| CPU 0/1/2/3 busy | 15/98/20/99% | 50/77/8/14% | 35/76/13/31% |

The two after runs cluster within 3.0% for Firefox CPU time and within 2.8% for
context-switch rate.  They are substantially below the original sample, but
the work phase also changed: the sampled DOM Worker consumed 3.010 CPU-s in the
baseline and only 0.350/0.300 CPU-s in the after runs.  With one old baseline,
this is not enough to claim that log suppression caused a 45% CPU reduction.
The defensible result is narrower: the identified serial warning storm is gone,
and the two post-change runs are mutually consistent.

The final capture's dominant Firefox thread was still the main thread at 7.940
CPU-s, followed by Renderer at 0.600 CPU-s, DOM Worker at 0.300 CPU-s, and IPDL
at 0.290 CPU-s.  Xorg stayed below 0.04 average cores.  This continues to argue
against scheduler pinning or Xorg tuning as the next optimization.  The main
browser thread is the highest-value target, while DRM/GPU acceleration remains
the expected architectural path for reducing software rendering and display
cost.

Synthetic first-rAF medians improved from 23/20/23 ms
(keyboard/pointer/scroll) in the baseline to 11/11/13 ms in the final run.
Tail latency remains unstable: final p95 was 27/114/73 ms.  These are useful
event-loop measurements, not physical USB-to-HDMI latency, and therefore do not
replace human interaction or display instrumentation.

The final info-level kernel-warning sample was empty.  Counts before and after
the workload were both zero for the ignored-`fadvise` and generic-unimplemented
messages.  The physically deployed image containing both changes is 6,035,544
bytes, SHA-256
`0239bf341d9c129ecd0e8e3032ad820e67734e2baa8658b8823d35598825120d`.
Its compressed transfer image has CRC32 `3f7a8e85`; the board verified all
kernel, Stage1, and DTB CRCs before boot.  After the non-equal `kcmp` return was
made explicitly orderless and requalified on both QEMU architectures, the
final source-tree release image is 6,035,688 bytes, SHA-256
`ac8f2033d01cf3517c277a36e384b1e8d6b438290cd1119e567e544a74f618eb`.

## Tooling improvements and next decision point

The browser capture now preserves its bounded interaction checkpoint when
strict navigation validation rejects Firefox's negative `fetchStart`.  The
system-time sampler records whether evidence is physical instead of
hard-coding QEMU provenance.  The startup profiler now waits for a complete
newline-terminated marker, writes a private structured JSON result, and has an
explicitly labelled local-I-cache diagnostic mode.  One hundred thirty-four
focused Python tests pass for the browser capture, system-time ledger, latency
contract, fixture, interaction, Firefox diagnostic, and board-session paths.

The first attempt to load the rebuilt kernel also exposed two independent
U-Boot transaction defects before Asterinas was entered.  A single long
`setenv bootargs` exceeded the board's console line buffer and left U-Boot at a
continuation prompt.  After recovery, explicitly copying the assembled long
value with `fdt set /chosen bootargs` caused a U-Boot load-access fault and its
firmware reset path.  All kernel, Stage1, and DTB CRC checks had passed, no
partition was written, and the new kernel had not executed.  The generic board
runner now stages long boot arguments through commands bounded to 512 bytes
and relies on the standard `booti` environment-to-DTB fixup, matching the
previously successful physical path.  The regression covers both the bound
and the absence of the hazardous redundant FDT write; the complete board
session suite has 65 passing tests.

The immediate next optimization experiment should use a low-overhead sampled
PC plus runnable/wait ledger for the Firefox main thread.  The existing detailed
fault/syscall probe perturbs startup by 36%, so it is unsuitable as the default
profiler.  Page-cache batching and futex wake classification remain candidates,
not established CPU bottlenecks, until sampled evidence separates execution
time from blocked time.  Scheduler affinity changes are also deferred: the
baseline already placed the two hottest threads on different CPUs, and the
after captures no longer show two saturated harts.
