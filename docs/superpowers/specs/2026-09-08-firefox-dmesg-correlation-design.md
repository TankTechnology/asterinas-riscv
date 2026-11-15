# Firefox dmesg correlation design

Date: 2026-09-08
Status: approved conversational design, awaiting written-spec confirmation
Scope: local RISC-V QEMU diagnosis; no remote PR maintenance or physical-board claim

## Objective

Produce one bounded Firefox experiment that can identify the first failing boundary
between Marionette transport, Firefox parent/child actors, kernel IPC/lifecycle behavior,
and syscall wait state.
The experiment is diagnostic evidence, not browser acceptance and not a speculative fix.

Existing evidence establishes that an `ExecuteScript` request was completely sent
and no response header arrived before timeout.
The Firefox parent and several content processes continued to execute syscalls,
so the evidence does not support a whole-browser deadlock.
Existing logs cannot select TCP, Unix IPC, epoll/futex, process lifecycle,
or an unresolved Firefox operation as the cause.

## Approaches considered

1. Add an independent kernel-log capture level and correlate dmesg with existing diagnostics.
   This is the selected approach because it preserves a quiet serial console
   while retaining opt-in informational lifecycle records.
2. Keep warning-only capture.
   This is cheaper but omits the existing clone/exec/wait/exit records emitted at `info`.
3. Boot with `loglevel=info` and disable the console later.
   This needs no kernel parameter but allows asynchronous boot output to interfere
   with the serial command protocol and changes the experiment more broadly.

## Kernel capture control

Add `asterinas.klog_capture=<level>` to the logger component.
It accepts the same `0..8` and lowercase level names as `loglevel`.
Its default is `warning`, preserving the current minimum-retention behavior.
Malformed values are rejected by the command-line framework and leave the default.
Repeated values use the framework's last-value behavior.

`loglevel` remains the console threshold.
The effective upstream OSTD filter becomes the maximum of the console threshold
and the selected capture threshold.
The retained-record path receives every record admitted by this effective filter,
while console output continues to use only the console threshold.
The Firefox experiment uses:

```text
loglevel=off asterinas.klog_capture=info asterinas.syscall_diag=1
```

The option is diagnostic and default-preserving.
It does not dynamically resize the 256-record ring,
enable unbounded syscall logging,
or change `/proc/sys/kernel/dmesg_restrict`.

The implementation belongs in `aster-logger` and depends on the already existing
`aster-cmdline` component so command-line dispatch precedes logger initialization.
Parsing and effective-level selection must be pure-testable.

## Evidence collection

Build on the corrected Firefox actor instrumentation and the wide checkpoint collector.
Rebuild the diagnostic Firefox archive from the frozen Debian root image
rather than reuse the earlier pre-review actor archive.
Keep the page, profile, package inputs, sandbox policy, CPU/memory topology,
and selected `ExecuteScript` parameters fixed.

Before the selected request, a privileged guest controller starts unmodified
util-linux `dmesg --follow-new --raw` as a child.
The controller consumes its stdout continuously into a bounded in-memory/run-file archive.
It retains at most 4 MiB, continues draining after the limit,
and records discarded byte/line counts instead of blocking the reader.
Unexpected dmesg exit, nonzero status, a kernel-buffer loss report,
or discarded output marks kernel-log evidence incomplete.

The controller writes scalar-only markers through `/dev/kmsg` at these boundaries:

```text
collector_started
request_selected
request_enter
snapshot_before_start / snapshot_before_end
snapshot_during_start / snapshot_during_end
request_return or request_error
snapshot_after_start / snapshot_after_end
collector_stopping
```

Each marker contains version, phase, Marionette request ID where available,
Firefox PID, and diagnostic-client PID.
It contains no URL query, script body, response payload, environment,
memory contents, or nonce.
The kernel-assigned dmesg timestamp supplies the guest clock.

The same selected request receives a fixed 300-second transport budget.
This avoids the earlier actor experiment's ambiguous 28-second residual budget.
The outer experiment remains bounded and leaves enough time for startup
plus before/during/after collection.
Only one selected `ExecuteScript` is observed; the experiment does not continue
into three graphical acceptance cycles.

The three proc snapshots retain their current identity validation and finite limits.
They contain each selected thread's current and last completed syscall,
sequence and guest jiffies, plus bounded fd metadata.
Firefox actor markers retain only stage, request ID, browsing-context ID,
source PID and target PID.

At shutdown the controller terminates only its exact dmesg child,
waits with a finite grace period, and uses a targeted kill if required.
It exports compressed, indexed frames over the existing serial protocol,
with decoded size and SHA-256 validation on the host.
The guest `/run` file is supporting storage, not the sole evidence path.

## Correlation and classification

The host produces an immutable evidence directory containing:

- complete serial transcript;
- decoded raw dmesg stream and collection metadata;
- before/during/after proc snapshots;
- validated Firefox actor and Marionette transport records;
- ordered correlation timeline;
- input/source SHA-256 manifest;
- a classification JSON with observations and claim boundaries.

Guest monotonic values, kernel jiffies and host receipt times are recorded separately.
They are not numerically compared across clock domains.
Within the guest, dmesg timestamps and proc jiffies may be ordered at millisecond precision;
record order and request IDs break ties where available.

The classifier applies the following bounded rules:

| First absent or contradictory boundary | Supported conclusion | Next probe |
| --- | --- | --- |
| Marionette send completes; no driver-entry actor marker | Failure is before or at Firefox command dispatch; no kernel wait primitive selected | Inspect transport/handler dispatch only |
| Parent actor submits; no child receipt | Parent-to-content delivery is the first missing actor boundary | Correlate the target IPC thread, Unix socket and epoll object |
| Child receipt exists; no script completion | Operation remains unresolved inside the content side | Use the target content thread's current syscall and browser stack stage |
| Child completion exists; no parent completion | Reply delivery/wakeup is the first missing boundary | Probe one identified Unix IPC/epoll path |
| Matching process exit plus wait/reap event | Lifecycle transition is correlated, not inferred from `ECHILD` text alone | Reproduce the exact parent/namespace/wait relation in a microtest |
| Request returns | Diagnostic run did not reproduce the same failure | Do not claim a fix; compare observer-on/off before further inference |
| Missing snapshot, dmesg loss/truncation, identity change, or short request budget | Evidence is incomplete | Repair only the collector boundary and rerun |

An isolated futex, ppoll, epoll or recvmsg snapshot is not classified as a missed wakeup.
A kernel primitive is selected only when the same target thread/object remains implicated
across actor position, syscall state and lifecycle evidence.

## Testing and verification

Before a full Firefox run:

1. Unit-test capture-level parsing, default behavior,
   and `effective = max(console, capture)` with failure-first tests.
2. Run the existing 12 store tests and the kernel-log micro guest.
   Extend the micro guest to prove `loglevel=off` plus capture `info`
   retains an `info` record without printing it.
3. Unit-test dmesg framing, byte limits, early exit, malformed frames,
   duplicate/missing phases, digest mismatch, and cross-clock rejection.
4. Exercise the exact cached util-linux binary with `--follow-new`
   in a short micro guest and acknowledge two post-start records.
5. Run the existing actor transformer and snapshot collector tests,
   then package a new immutable actor root image with a source manifest.
6. Run one complete QEMU experiment only after all short gates pass.

The final Firefox result is acceptable as diagnostic evidence only if
all input hashes match, the selected request has the full 300-second budget,
all three snapshot frames validate, dmesg remains live without loss/truncation,
and the actor/transport parsers report no malformed or ambiguous record.

If instrumentation changes the failure, do not declare Firefox fixed.
Run one observer-disabled control with otherwise identical inputs only then.
No repeated full run is scheduled merely because the classification is inconvenient.

## Non-goals and limits

- No physical-board execution, PR update, commit, dependency installation,
  container/image deletion, Firefox repair, or kernel semantic fix is authorized here.
- No full syscall trace, ptrace/strace, eBPF, tracefs, journald guarantee,
  user payload logging, or unbounded serial printing.
- The log ring remains bounded and can overwrite records;
  the continuously draining dmesg child and explicit incompleteness checks mitigate this,
  but do not make logging crash-persistent.
- Reader notification still depends on timer progress.
- A successful correlation narrows the next kernel probe;
  it does not by itself prove a kernel defect.
