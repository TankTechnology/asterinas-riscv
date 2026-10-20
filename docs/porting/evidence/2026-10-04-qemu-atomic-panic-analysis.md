# QEMU Debian workload atomic-mode panic analysis

Date: 2026-10-04 (Asia/Shanghai)

This is a source-level follow-up to the failed Debian M5 slirp run recorded in
the [QEMU recovery matrix](2026-10-03-qemu-recovery-matrix.md). It records the
fresh-image QEMU rerun as well; it does not claim a physical-board result.

## Observed failure

The retained serial log is
`target/debian-riscv/desktop-m5-network/qemu-gate-current-20261004/desktop-m5-qemu.serial.log`.
The run completed twenty 64 KiB fixture transfers and DNS/HTTPS, then recorded
these ordered diagnostics on CPU 3:

```text
MIGHT_SLEEP_VIOLATION ... preempt_count=1 ...
MIGHT_SLEEP_VIOLATION ... preempt_count=2 ...
RefCell already borrowed
... heap-allocator/src/allocator.rs:300
This function might break atomic mode ... outstanding guard ...
... kernel/comps/block/src/bio.rs:308
```

The source location at `bio.rs:308` is `BioMetadata::wait`, which waits on a
`WaitQueue` until the block request completes. The immediate guard location in
the panic text is `ostd/src/sync/guard.rs:42:9`, the `PreemptDisabled` guardian
used to take a spin-based lock. The first atomic-mode report then formatted the
guard's `Location` with `alloc::format!`; that allocation re-entered the local
allocator while the panic path was already active and obscured the original
call stack with `RefCell already borrowed`.

This identifies a diagnostic recursion bug and narrows the functional failure
to a block wait reached while a preemption-disabling guard is still live. It is
not yet proof of which higher-level lock was retained by the caller; the
current old artifact does not emit enough of the original stack before the
recursive panic.

## Source change and checks

`ostd/src/task/atomic_mode.rs` now emits the outstanding guard file, line, and
column directly with `early_println!`, then panics using only scalar fields.
The violation path no longer calls `alloc::format!` or constructs an allocated
fallback string. A source contract test
`tools/riscv/tests/test_atomic_mode_diagnostics.py` first failed against the
old implementation and passes against the change.

Fresh checks on the current worktree:

```text
python3 -m unittest tools.riscv.tests.test_atomic_mode_diagnostics \
  tools.riscv.tests.test_qemu_uboot_contracts \
  tools.riscv.tests.test_qemu_uboot_booti -v
Ran 243 tests in 6.284s
OK (skipped=1)

cargo check -p ostd --target riscv64imac-unknown-none-elf
Finished `dev` profile

cargo check -p aster-kernel --target riscv64imac-unknown-none-elf
Finished `dev` profile
```

The cargo checks were run with a separate target directory and validate the
current source, but they do not produce the OSDK boot image used by the old
Debian gate. The fresh-image rerun below is therefore kept as a separate,
hash-identified result rather than being inferred from the compile checks.

## Fresh-image result

The follow-up build was produced in
`target/atomic-mode-osdk/aster-kernel-osdk-bin.Image` with SHA-256
`6ccd523dafd177e4ba4a844e3cb5e4abc667141ef9825f370f7d4e7280e5b455`. The same
bounded Debian M5 QEMU gate then published:

```text
target/debian-riscv/desktop-m5-network/qemu-gate-atomic-20261004/result.json
passed=true
reason=pass
network_fixture.request_count=20
DEBIAN_NETWORK_M5_QEMU_HTTPS status=200
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

The retained serial log contains no `MIGHT_SLEEP_VIOLATION`,
`ATOMIC_GUARD_TAKEN`, `RefCell already borrowed`, `PANIC_ENTRY`, or
`Uncaught panic` markers. QEMU was cleaned up by the gate. This is evidence that
the diagnostic change removed the observed recursive panic and that the
current source image completes this workload; it is not a physical-board
qualification and does not establish that every atomic-mode wait caller has
been corrected.
