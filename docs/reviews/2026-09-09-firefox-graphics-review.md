---
date: 2026-09-09
mode: diff
base: cc66b9d0b
head: b66fcc696
branch: codex/megrez-physical-graphics-current-main
title: "Firefox graphics diagnostic loop review"
---

# Summary

The review found 13 issues in `b66fcc696`: two maintainability findings,
eight correctness findings, and three documentation findings. Follow-up commits
`9a13a1bd4` and `dc867074d` resolve 11 of them: required diagnostic source is
tracked; installed source and evidence windows are verified fail-closed; the
optional Firefox fixture is skipped cleanly; TCP dequeue logging is placed at
the actual dequeue boundary; shared-memory keys and concurrent detach are
covered by regressions; optional syslog diagnostics are gated; and the Linux
compatibility documentation is updated.

Two findings remain explicitly recorded rather than hidden. The original
`b66fcc696` is too broad to retroactively make atomic without rewriting shared
history. More importantly, System V shared-memory attachment accounting is not
yet tied to all VM lifecycle paths (`fork`, `exec`, process exit, arbitrary
`munmap`, or `SHM_REMAP`). The Firefox/Xorg blocker uses explicit `shmdt` and is
covered by the current experiment, but broader lifecycle conformance remains
future kernel work and must not be inferred from this milestone.

Verification after the follow-up fixes: the cached/offline RISC-V kernel build
passed, the System V SHM raw probe passed in RISC-V QEMU, C syntax and formatting
checks passed, and all 1,362 RISC-V Python tests passed with one optional fixture
skip. The final plan-bound desktop and physical-board gates are tracked
separately from this code review.

## Maintainability

### `commit b66fcc696 message`

> ```diff
> fix(riscv): close Firefox graphics diagnostic loop
> ```

`atomic-commits` (major): This commit combines a new kernel-log subsystem, shared-memory lifetime and permission changes, a TCP receive fix, Firefox instrumentation, graphics acceptance changes, and unrelated formatting. These independently reviewable changes cannot be bisected or reverted separately.

**Fix.** Split the independent changes into commits, placing preparatory interface refactoring before the features that consume it.

### `tools/riscv/diagnostics/firefox_dmesg_experiment.py` line 25

> ```diff
> DEFAULT_BASELINE = (
>     REPOSITORY
>     / "target/firefox-diagnostics-20260908/browser_checkpoint_experiment-wide.py"
> )
> ```

`coupling-cohesion` (major): The runner imports executable source from an untracked file under `target/`. Its exact hash is mandatory, and both preparation and several tests call `load_baseline()` unconditionally. A clean checkout cannot run this workflow, and the implementation cannot be reconstructed from this commit.

**Fix.** Commit the required baseline implementation as a source module or extract its reusable functionality into the diagnostic package. Keep generated evidence under `target/`, rather than required executable source.

## Correctness

### `kernel/libs/aster-bigtcp/src/iface/poll.rs` line 468

> ```diff
> if !tcp_repr.payload.is_empty() {
>     let key = socket.connection_key();
>     record_tcp_diagnostic(
>         TcpDiagnosticStage::PendingPop,
> ```

Missing diagnostic events (minor): `PendingPop` is emitted only inside the packet-generation callback and only for nonempty payloads. A socket successfully removed by `pop_pending_tcp()` can generate no packet, as the preceding comment explicitly allows. Such a dequeue produces no `pending-pop` observation, making this supposedly separate diagnostic boundary indistinguishable from payload generation.

**Fix.** Record `PendingPop` immediately after a successful `pop_pending_tcp()`, and leave `SegmentGenerated` in the dispatch callback. Test a dequeued socket that generates no payload.

### `kernel/src/ipc/ipc_ns.rs` line 238

> ```diff
> self.shm_ids.remove_if(shmid, |shm_set| {
>     may_remove(shm_set)?;
>     Ok(shm_set.mark_for_removal())
> })?;
> ```

Stale shared memory key (major): Marking an attached segment for removal preserves its original key association. After `shmget(key, ...)`, `shmat()`, and `IPC_RMID`, another `shmget(key, ..., IPC_CREAT | IPC_EXCL)` still returns `EEXIST`; without `IPC_EXCL`, it finds the removed segment. Linux immediately removes the key association while retaining access by ID. See [Linux's removal implementation](https://github.com/torvalds/linux/blob/v6.16/ipc/shm.c#L115-L128).

**Fix.** Separate key lookup from ID allocation. Remove the key association atomically when marking a segment, while retaining its ID until the last attachment disappears. Test recreation using the same key before the old segment's final detach.

### `kernel/src/ipc/shared_memory/shm.rs` line 124

> ```diff
> let removed_shmid = ipc_ns.remove_shm_attachment(pid, shmaddr);
> debug_assert_eq!(removed_shmid, Some(shmid));
> ipc_ns.release_shm_attachment(shmid, pid)?;
> ```

`atomic-critical-sections` (major): Two threads calling `shmdt()` on the same address can both obtain the attachment and query its mapping before either unmaps it. Both subsequent `remove_mapping()` calls succeed, including the second call on the empty range. The second attachment removal then returns `None`, triggering the debug assertion; release builds instead decrement `nattch` twice. With another attachment still alive, this can prematurely destroy a segment marked for removal.

**Fix.** Serialize attachment lookup, mapping removal, and attachment-count release as one transaction. Ensure only the thread that owns the attachment removal can decrement its count, with rollback on failure.

### `kernel/src/ipc/shared_memory/shm_set.rs` line 125

> ```diff
> pub fn mark_for_removal(&self) -> bool {
>     self.marked_for_removal.store(true, Ordering::Release);
>     self.nattch.load(Ordering::Acquire) == 0
> }
> ```

`raii` (major): Deferred destruction now relies on `nattch` reaching zero, but attachments are released only by explicit `shmdt()` or failed `shmat()`. A process can create and attach a segment, mark it with `IPC_RMID`, touch its pages, and exit without detaching. VM teardown never releases this count, so the IPC namespace retains the segment and its pages indefinitely. Repeating this leaks kernel resources.

**Fix.** Tie attachment accounting to VM mapping lifetime so exit, exec, unmapping, and mapping replacement release attachments automatically. Account for inherited mappings during fork, and add lifecycle regression tests.

### `test/initramfs/src/regression/process/syslog/syslog.c` line 330

> ```diff
> read_marker(fd, marker, &priority);
> TEST_RES(priority, _ret == 6);
> ```

Test requires disabled diagnostics (major): The process regression runner invokes this executable unconditionally, but this test requires a clone lifecycle record that exists only with `asterinas.syscall_diag=1` and informational capture. Diagnostics default to disabled, and the normal regression boot does not enable them. Consequently, `read_marker()` times out and fails the standard regression suite.

**Fix.** Move this assertion into the explicitly configured kernel-log diagnostic test, or gate it on an explicit fixture setting. Keep the normal syslog regression independent of optional lifecycle logging.

### `tools/riscv/diagnostics/firefox_dmesg_experiment.py` line 503

> ```diff
> "guest_helper_sha256": sha256(GUEST_HELPER),
> "guest_source_sha256": hashlib.sha256(generated_source.encode()).hexdigest(),
> ```

Incorrect source provenance (major): The manifest hashes freshly generated host source, but `guest_loader()` executes the already installed `/usr/lib/asterinas/firefox-dmesg-driver.py`. Nothing compares that installed driver or helper with the current host files. Reusing a previously prepared root image after changing the generator or helper therefore records source hashes for code that did not execute.

**Fix.** Verify the installed driver and helper hashes against the expected source before running, or record their actual hashes from the prepared image and reject mismatches. Bind the source manifest to the executed payload.

### `tools/riscv/diagnostics/firefox_dmesg_records.py` line 469

> ```diff
> "browser_acceptance": False,
> "evidence_complete": True,
> "request_id": request_id,
> "outcome": outcome,
> ```

Incomplete evidence accepted (major): `correlate()` reports `evidence_complete: true` without requiring collection markers, a terminal request outcome, or usable snapshot identities. A replay with empty dmesg bytes, one `send_complete` record, and three snapshots containing `root_identity: {}`, empty process lists, and `time_limit` is accepted as `boundary_missing`. This was reproduced; the existing tests still pass.

**Fix.** Validate identity fields and their agreement across sources, require usable snapshots and the expected collection/request markers, and distinguish interrupted observation from a completed observation window. Return incomplete evidence when those prerequisites are absent.

### `tools/riscv/tests/test_firefox_actor_diagnostics.py` line 146

> ```diff
> def test_transport_module_marks_callback_parser_and_dispatch_boundaries(self):
>     with zipfile.ZipFile(ARCHIVE) as archive:
> ```

Missing optional fixture guard (minor): This test and `test_server_unbuffered_variant_is_explicit_and_default_is_unchanged()` open the cached Firefox archive unconditionally. Unlike `PackagedActorTests`, they have no archive-availability guard. Both raise `FileNotFoundError` in a checkout without the optional archive, which was reproduced by substituting an absent archive path.

**Fix.** Apply the archive-availability guard to both tests, or provide committed minimal fixtures for these transformations.

## Documentation

### `book/src/kernel/linux-compatibility/syscall-flag-coverage/system-information-and-misc/README.md` line 108

> ```diff
> At least warning-level messages are captured after installation,
> even when the boot console is disabled;
> more verbose capture follows the boot log level.
> ```

Incorrect capture guarantee (minor): The unconditional guarantee that warning-level messages are captured contradicts the new configuration. With `loglevel=off asterinas.klog_capture=off`, `effective_level_filter()` returns `0`, so warnings are not captured. Informational capture can also be enabled independently of the boot console level.

**Fix.** Describe warning capture as the default, and explain that the effective threshold is the more verbose of `loglevel` and `asterinas.klog_capture`.

### `kernel/src/net/mod.rs` line 12

> ```diff
> aster_cmdline::define_kv_param!("asterinas.tcp_diagnostic_port", TCP_DIAGNOSTIC_PORT);
> ```

`linux-compat-docs` (minor): The new user-visible `asterinas.tcp_diagnostic_port` kernel parameter has no entry in the Kernel Parameters documentation. Users cannot discover its disabled default, accepted port range, tracing budget, or logging requirements there.

**Fix.** Document the parameter in `book/src/kernel/linux-compatibility/kernel-parameters.md`, including its valid values, default, bounded output, and required capture level.

### `kernel/src/syscall/shmctl.rs` line 36

> ```diff
> ipc_ns.mark_shm_set_for_removal(shmid, |shm_set| {
> ```

`linux-compat-docs` (minor): The shared-memory permission and deferred-removal enhancements have no matching compatibility documentation or SCML update. The compatibility table still labels `shmget`, `shmat`, `shmctl`, and `shmdt` unsupported, and the coverage pages contain no shared-memory coverage entries.

**Fix.** Update the shared-memory compatibility table and add the matching coverage descriptions and SCML, including deferred `IPC_RMID`, attachment permissions, capability overrides, and remaining limitations.
