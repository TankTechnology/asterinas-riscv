# Firefox Targeted Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a browser timeout into correlated transport, outstanding-syscall, and process-lifecycle evidence, with a small Linux-reference IPC experiment.

**Architecture:** Add an opt-in, constant-size per-thread syscall snapshot, exported through a read-only, access-checked Asterinas-specific proc file. A bounded guest collector samples only the selected process tree and reports missing facilities explicitly; Marionette reports framing progress without exposing payloads. Keep diagnostic collection independent of browser acceptance and do not change socket, scheduler, signal, or VM semantics.

**Tech Stack:** Safe Rust kernel/procfs, Python standard library, C/POSIX regression program, existing persistent Docker environment.

---

## Scope and execution

User approved this scope on 2026-09-08 after reviewing the blocking-chain analysis.
Reuse `/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-physical-graphics-current-main` and its persistent container.
No image removal, package download, unrelated PR integration, physical board boot, or change to trusted-input acceptance.
Preserve all existing worktree changes; new work remains uncommitted until review.
Use one isolated implementation subagent for the kernel/procfs unit while the controller works on guest diagnostics and the independent C probe.

## Task 1: Opt-in syscall state and lifecycle evidence

**Files:** Create `kernel/src/syscall/diagnostics.rs` and a pure state helper underneath it; update syscall dispatch, POSIX thread state/builder, procfs task entries, kernel parameter documentation, and narrowly placed exit observation if required.

- [x] Write and run host Rust tests against the actual pure state helper before implementation.
  Cover an outstanding call, completed result, consecutive sequences, a no-return/context-replaced outcome, and an empty snapshot.
  The following observable sequence is the minimum contract:

```text
begin(number=212, args=[7,0,0,0,0,0], jiffies=10)
snapshot -> current.number=212, current.sequence=1, completed=null
finish(result=-11, jiffies=12)
snapshot -> current=null, completed.number=212, completed.result=-11
begin(number=22, args=[8,0,1,0,0,0], jiffies=20)
snapshot -> current.number=22, current.sequence=2, completed.sequence=1
```

- [x] Implement `asterinas.syscall_diag=1`, default off.
  With the switch off, no snapshot allocation, timer read, or formatted output occurs on syscall entry/return.
  With it on, keep only the current call and last completion per thread, in lazily allocated storage; no global PID hash table or per-call serial printing.
  This small kernel bookkeeping is system-wide while enabled; collection/export is targeted in userspace.
- [x] Export `/proc/<pid>/task/<tid>/asterinas_syscall` and the main-thread alias.
  JSON schema: `version`, `enabled`, `pid`, `tid`, `snapshot_jiffies`, `current`, `completed`.
  A call contains `sequence`, `number`, six scalar `args`, `entered_jiffies`;
  completion also contains `finished_jiffies`, `result` (integer or null), and `outcome` (`return`, `error`, or `no_return`).
  State is coherent, immutable for one open/read sequence, and protected by the existing procfs alien-read permission/VM identity pattern.
  No new Linux `/proc/.../syscall` compatibility claim is made.
- [x] Observe clone/exec/wait boundaries and normal/signal-driven exits with bounded, opt-in OSTD lifecycle messages outside state locks.
  Include global PID/TID, parent identity, syscall/return or termination status, and explicit suppression accounting.
  Do not copy user strings, buffers, credentials, or environment data into messages.
- [x] Test proc snapshot formatting, disabled mode and offset/EOF behavior; compile the current RISC-V kernel offline using existing outputs.
- [ ] Exercise live cross-credential/FD-transfer access checks and exec-race protection beyond source review. These are explicitly not covered by the host helper tests or the initial QEMU smoke test.

## Task 2: Bounded process-tree snapshots

**Files:** Create `tools/riscv/debian/rootfs/firefox_diagnostic_snapshot.py` and `tools/riscv/tests/test_firefox_diagnostic_snapshot.py`.

- [x] Start with temporary proc-tree fixtures and failing tests for a selected root, child, unrelated process exclusion, all selected TIDs, and resource limits.
- [x] Implement a standard-library CLI accepting a positive root PID and an explicit process/thread/file-byte budget.
  Read only identity/status/comm plus the new diagnostic file and bounded fd/fdinfo information.
  Never collect environment variables or arbitrary memory.
  Each unavailable file must distinguish `unsupported`, `process_gone`, `permission_denied`, `io_error`, `too_large`, and invalid diagnostic JSON.
  Report truncation when process, thread, fd, byte, or time budgets are exhausted.
- [x] Emit one JSON record with `version=1`, `physical=false`, root identity, capture duration, process/thread records, and completeness reasons.
  Host runner or shell enforces an outer timeout; finite reads use nonblocking descriptors and bounded lengths where applicable.
  Do not rely on Marionette, systemctl, or ps.
- [x] Run the fixture tests and a bounded native Linux snapshot to verify that absent Asterinas interfaces are reported as unsupported, not successful empty evidence.

## Task 3: Marionette frame progress

**Files:** Update `browser_m5_marionette_gate.py` and add focused `test_marionette_diagnostics.py` (preserving existing changes).

- [x] Add a real socket-pair regression verifying the sent command and only a partial response before the deadline.
- [x] Under `ASTERINAS_MARIONETTE_DIAGNOSTICS=1`, emit request identity/name, send completion, frame prefix/body progress and exact failure stage/received-byte counts.
  Do not log the script, URL query, nonce, screenshot, or other response payload.
  Diagnostic emission must not change deadlines, protocol validation or public error behavior.
- [x] Test no diagnostics by default, partial header/body, EOF, invalid framing, success with fragmented I/O, and failure of a closed diagnostic sink.

## Task 4: Minimal IPC and child-lifecycle reference probe

**Files:** Create a standalone C diagnostic under `tools/riscv/diagnostics/` and a focused host test/runner.

- [x] Define expected records before implementing the probe.
  Successful Linux reference must establish: initial nonblocking receive yields EAGAIN;
  a child sends a byte plus one valid descriptor over a Unix socket;
  epoll reports the receiver ready;
  recvmsg returns the expected byte and descriptor without MSG_CTRUNC;
  the received descriptor reads expected content;
  waitpid reports the child's chosen exit status;
  a second wait reports ECHILD.
- [x] Implement bounded waits and cleanup, establishing the empty receive before fork instead of timing sleeps or an extra handshake.
  Print identity, phase, expected/actual result and errno, without claiming Firefox or physical acceptance.
- [x] Compile and execute with the cached native C compiler; cross-compile with the cached Nix RISC-V compiler and dynamic libc (static libc is not cached).
  Record architecture explicitly and do not call native Linux success a RISC-V kernel pass.

## Task 5: Packaging, verification and review

- [x] Package the collector through the existing offline rootfs overlay; keep invocation opt-in and independent of the browser gate.
  Added explicit per-file creation permission for the new collector, root/byte/time/type verification, and fail-closed ext2 handling; original frozen input is preserved.
- [x] Run focused Python suites, actual Rust state tests, native C reference, RISC-V compile, formatting and diff checks.
- [x] Use a bounded QEMU microtest only if existing cached boot inputs support it without modifying immutable evidence.
  Otherwise report the unexecuted kernel runtime verification explicitly.
- [x] Obtain independent specification review, then code-quality/security review; repair blocking findings and re-review them.
  Non-blocking refactoring suggestions and pre-existing protocol-validation behavior remain documented follow-ups; this is not a merge-readiness claim.
- [x] Write evidence with commands, results, input identities, limitations and the next informative experiment.
  No physical acceptance or Firefox root-cause claim without corresponding runtime evidence.

## First-batch runtime checkpoint

See `docs/porting/evidence/2026-09-08-firefox-targeted-diagnostics.md`.
The four-hart RISC-V on/off microtest validates eight IPC phases in both modes,
a live outstanding sleep snapshot, disabled snapshots, normal/signal exits, and ECHILD outcomes.
It is not a Firefox or physical-board test and does not close the live credential/exec-race checklist above.
