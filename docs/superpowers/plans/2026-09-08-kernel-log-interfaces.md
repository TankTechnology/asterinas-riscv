# Kernel log interfaces implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the ABI adapter and independent reviews; the controller implements the shared record store. Preserve all pre-existing worktree changes. Do not commit or mutate remote PRs during this local integration.

**Goal:** Provide a bounded kernel log record store, Linux `/dev/kmsg` and `syslog(2)` access, and small repeatable tests for later system debugging.

**Architecture:** `aster-logger` owns record storage and console policy; the safe kernel owns credentials, user copies, device cursors, syscalls and reader notifications. Recording is allocation-free under an IRQ-safe lock; formatting occurs before that lock. User copying and waking readers never occur under the record lock or directly on the logging path. A timer callback delivers coalesced readiness notifications outside that path.

**Tech Stack:** Safe Rust, current OSTD logging API, existing device/procfs interfaces, C syscall probes, cached Rust/C toolchains and persistent Docker.

## Provenance and design boundaries

- Reference https://github.com/asterinas/asterinas/pull/2265 at `2830167d362082dfac37900c9d38a9f355f1cce5` and successor https://github.com/asterinas/asterinas/pull/3288 at `706b90675ab0f884e8fd232ec41be5284f343ee8`.
- Reuse the architectural separation and console-policy lessons, not the old byte-stream implementation. Linux `/dev/kmsg` needs whole records, independent cursors, sequence numbers, overwrite detection and special seek semantics.
- The existing `ring-buffer` is a dynamically allocated SPSC destructive FIFO. A small fixed-slot record store is deliberately used here for allocation-free early logging and independently positioned readers, without changing that library or introducing unsafe code.
- Kernel records retain at most 1024 message bytes, with explicit truncation; 256 slots bound storage. Formatting and reads are bounded. Console filtering never changes storage filtering.
- Preserve the boot-selected console verbosity. Retain at least warnings in memory even when the boot console is off; higher-volume capture remains controlled by the existing boot log level. Early logs emitted before logger injection are outside this first milestone.
- `dmesg_restrict` defaults to true. Reuse credential capabilities and implement genuine errors. No payload collection or unprivileged kernel-address exposure is added intentionally; existing log contents require privileged access by default.
- No tracefs, perf, eBPF, Firefox rerun, physical boot, dependency installation or Docker cleanup is part of this milestone.

## Task 1: Record store and logger integration (controller)

Files: create `kernel/comps/logger/src/klog.rs`, `kernel/comps/logger/src/klog/store.rs`, `kernel/comps/logger/src/klog/store_tests.rs`; modify logger `lib.rs` and `aster_logger.rs`.

- [x] Write Rust tests against the real store for empty reads, two independent sequence cursors, whole-record overwrite, clear-marker independence, escaping, truncation and console-policy transitions. Observe failing tests before implementing the corresponding fixes.
- [x] Implement a pure fixed-size record store and allocation-free bounded formatter. Maintain its 12 tests through `make test_klog_store`, also wired into `make test`.
- [x] Add IRQ-safe `KernelLog` access. Public contract: `klog()`, `bounds() -> (first, next, cleared)`, `record(sequence) -> Result<LogRecord, ReadError>`, `push(priority: u16, message: &[u8])`, `clear_to(sequence)`, `take_pending()`, console controls, restriction getter/setter, and capacity query. `ReadError` distinguishes `Empty` from `Overrun(first_sequence)`. Records expose formatting methods `format_kmsg` and `format_syslog` accepting `fmt::Write`; `write_syslog` preserves arbitrary bytes for the syscall ABI.
- [x] Integrate with OSTD logger; format once before store lock, store before independently filtered console output. Do not wake scheduler from `log()`.

## Task 2: Linux ABI adapters (implementation agent)

Files: add a kernel log device module, syscall module and `dmesg_restrict` proc file; minimally adjust device/proc registration, syscall dispatch and per-open seek support. Own only these kernel files; controller owns logger component.

- [x] Add a C regression probe before implementation. Check missing support on a baseline guest when feasible; pure store tests are not ABI validation.
- [x] `/dev/kmsg`: major 1 minor 11, per-open serialized cursor, read one whole escaped record, `EAGAIN` when nonblocking/empty, interruptible blocking wait, `EPIPE` and cursor repair on overwrite, `EINVAL` for insufficient record buffer, Linux special seeks. Implement user writes with priority/facility validation and no forged kernel facility.
- [x] Coalesce pending logger notifications from an initialized kernel timer callback into a `Pollee`; no logging inside the notifier. Review reentrancy and IRQ constraints.
- [x] `syslog(2)`: actions 0–10, correct capability checks, signed length validation, independent destructive-read cursor and non-destructive clear boundary, console off/on/level independent of capture. Register x86 and generic syscall numbers. Keep user copies outside spinlocks and preserve interruption semantics.
- [x] Add `dmesg_restrict` using existing sysctl patterns; default restricted. Do not weaken other controls.

## Task 3: Integration, verification and review

- [x] Run actual-store tests, C compile with warnings as errors, and incremental offline RISC-V kernel build via the persistent launcher.
- [x] Use one small QEMU initramfs workload for ABI tests and unmodified cached `dmesg`, not Firefox. Cover independent readers, nonblocking emptiness, small buffers, seeks, wakeup, rollover, syslog clear/read independence and permissions. Record unsupported or untested outcomes honestly.
- [x] Perform specification review followed by maintainability/development/security review. Fix confirmed defects with regression tests; rerun relevant checks.
- [x] Write a local usage/evidence note with exact commands, limits, input provenance and test results. Do not claim journald or physical-board support without running them.

## Completed local result

See [the evidence and usage note](../../porting/evidence/2026-09-08-kernel-log-interfaces.md).
The final frozen RISC-V kernel and micro initramfs passed on SMP1 and SMP4:
65 C assertions per boot, real kernel warning retention with quiet console,
console controls, BusyBox and util-linux dmesg, and two follow acknowledgments.
The 12 store tests and 12 gate-validator tests pass.
The full CI suite, physical deployment and Firefox experiment were not run.
SCML documentation was updated, but its official offline validator was unavailable
because the `nom` dependency was not cached; no download was attempted.
