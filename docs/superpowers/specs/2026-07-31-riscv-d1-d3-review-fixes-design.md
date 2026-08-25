# RISC-V D1 and D3 Review Fixes Design

## Goal

Resolve the confirmed pre-submission findings in the RISC-V page-table
activation and firmware-selected DW APB UART branches without introducing an
asynchronous logging subsystem or a general Devicetree address translator.

No implementation commit, rebase, push, or pull request is allowed before the
user reviews the resulting diff and test evidence.

## D1: Preserve Global TLB Entries

`activate_page_table` must still execute an `SFENCE.VMA` immediately after
writing `satp`, because every current address space uses ASID 0 and a `satp`
write does not invalidate cached translations.

Ordinary page-table activation will call
`tlb_flush_all_excluding_global()`. On RISC-V, that helper will encode:

- `rs1 = x0`, selecting every virtual address; and
- `rs2` as a non-`x0` register containing ASID 0, selecting ASID 0 while
  preserving global translations.

The implementation must use inline assembly because the current `riscv` crate
does not expose this exact operand encoding. Calling
`riscv::asm::sfence_vma(0, 0)` is incorrect: both operands would be register
operands, so `rs1` would not be encoded as `x0`.

`activate_kernel_page_table()` retains its existing
`tlb_flush_all_including_global()` after first activation. This remains the
only activation path that deliberately discards global translations.

## D3: Restore an IRQ-Safe Direct Console Path

The console registry and logger changes in commit `a3bd9731d` will not be part
of the final D3 series. The existing registry spinlock and whole-record logger
serialization remain unchanged.

The DW APB UART will follow the established NS16550A model:

- transmitter ownership uses an IRQ-disabling spinlock;
- every caller, including IRQ and other atomic contexts, uses the same direct
  transmit path;
- no caller silently reports success after discarding the supplied bytes;
- the transmitter-ready poll remains bounded;
- firmware baud rate, line control, FIFO, clocks, resets, and interrupts remain
  untouched; and
- the existing RISC-V I/O fence remains around ownership handoff because Rust
  atomic ordering does not order the MMIO I/O domain.

The lock guard must be released before fatal-error handling. A three-state
atomic failure value (`healthy`, `reporting`, `reported`) elects exactly one
failure owner. For an MMIO access failure, that owner marks `reporting` while
it still owns the transmitter, releases the lock guard, prints the panic stack
trace through the SBI-backed early console, publishes `reported`, and aborts.
For a transmitter-readiness timeout, the owner must not call the SBI console,
because firmware may poll the same stuck UART indefinitely; it publishes
`reported` and aborts without further output. Other harts that observe either
failure state perform no further DW APB MMIO and wait only for `reported`
before aborting.

This design deliberately does not add a log queue, worker task, deferred
flusher, per-CPU buffer, or fallible console API.

## Devicetree Address Contract

The first upstream version supports only UART nodes whose `reg` address is
already CPU-addressable through identity-mapped ancestor buses.

For a selected `stdout-path`:

1. Strip the optional serial-options suffix.
2. Resolve an alias to its absolute node path, or retain an absolute path.
3. Inspect every ancestor bus between the UART and the root.
4. Accept an empty `ranges` property as an identity mapping.
5. Reject a missing or non-empty `ranges` property instead of interpreting the
   child-bus `reg` address as a CPU physical address.

Megrez and QEMU virt both use an empty `/soc/ranges`, so this fail-closed rule
preserves the intended platforms. General non-identity `ranges` translation is
a separate cross-driver infrastructure project.

The legacy first-NS16550A fallback remains unchanged when `stdout-path` is
absent.

## Configuration and File Shape

`DwApbConfig::from_node` owns missing and malformed-property errors.
The lower validation helper accepts concrete `reg_shift` and `reg_io_width`
values, so its type does not represent production states that have already
been rejected.

The implementation should become smaller after removing the sleeping ownership
and failure-waiter machinery. Tests may move to `dw_apb/tests.rs` only if the
main driver file remains difficult to navigate after simplification; file
splitting is not an independent goal.

## Test Strategy

Development follows red-green-refactor.

### D1

- First build the current RISC-V kernel and run a disassembly assertion that
  fails because the post-`satp` instruction is the global
  `sfence.vma x0, x0`.
- Implement the exact ASID-0, all-address, non-global fence.
- Rebuild and require disassembly to show `rs1 = x0` and `rs2 != x0`.
- Run RISC-V OSTD kernel tests, Sv39 and Sv48 builds, and unaffected-architecture
  builds.

QEMU address-space behavior is not a valid regression oracle here because it
eagerly invalidates translations on `satp` writes.

### D3

- Add failing tests that require atomic callers to transmit rather than discard
  bytes and that require ownership serialization without a sleeping mutex.
- Add failing tests that elect one fatal-error reporter, prevent later MMIO,
  and publish the stack-trace completion state before secondary aborts.
- Add failing pure tests for absolute and alias `stdout-path` resolution and
  rejection of missing or non-empty ancestor `ranges`.
- Simplify the driver until those tests pass.
- Run the focused RISC-V UART kernel tests, `make check`, RISC-V and LoongArch
  builds, and the RISC-V SMP QEMU fallback boot.
- Run a fresh Megrez boot only after the host-side matrix is green. Require one
  DW APB registration message and a deterministic userspace marker.

## Branch and Review Procedure

Implementation is prepared as uncommitted changes in the existing D1 and D3
worktrees. After the user approves both diffs:

- rebase each branch onto the then-current `upstream/main`;
- reconstruct the D3 history without `a3bd9731d`;
- rerun the final verification matrix;
- present the rebased commits for one last review; and
- commit or push only after explicit user approval.
