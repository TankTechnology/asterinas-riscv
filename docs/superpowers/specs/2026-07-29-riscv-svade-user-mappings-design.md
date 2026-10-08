# RISC-V Svade User-Mapping A/D Handling Design

## Status

The scope and recommended architecture were approved by the user on
2026-07-29. This written specification is awaiting user review. No
implementation code has been written, staged, or committed.

PR2 is developed as a local stacked branch on top of PR #3657. It will not be
published until PR #3657 is merged and PR2 is rebased onto the resulting
upstream `main`, unless the maintainers explicitly request a stacked PR.

## Problem

On RISC-V systems using Svade, the MMU raises a page fault when a valid leaf
PTE has `A=0`, or when a write targets a leaf with `D=0`. This is not
necessarily a permission failure. It can be a request for the operating
system to record the access in the PTE and retry the instruction.

PR #3657 makes boot and kernel mappings ready for Svade. User mappings still
have two distinct requirements:

- tracked user RAM deliberately retains meaningful A/D status, so an existing
  mapping may need A/D repair in the normal VM fault path;
- user I/O mappings are not tracked for access or dirty status and their
  current fault path rejects faults, so they must receive usable A/D status
  before the CPU accesses them.

Here, "user" means a user-mode process and its virtual address space. Managing
that address space, validating its permissions, updating its page tables, and
resuming a faulting instruction are kernel responsibilities.

## Goals

1. Resolve legitimate Svade A/D faults on existing tracked user-RAM mappings.
2. Prevent unrecoverable Svade A/D faults on user I/O mappings.
3. Keep permission checks, copy-on-write behavior, and VM fault-around
   semantics unchanged.
4. Reuse PR #3657's typed `PageAccess` operation and atomic page-table
   protection path.
5. Preserve exact raw PTE restoration and non-RISC-V behavior.
6. Preserve existing kernel-fault panic and backtrace behavior.
7. Keep PR2 small: no new runtime module, raw PTE walker, or test harness.

## Non-Goals

- Changing trap decoding or the `PageFaultInfo` representation.
- Handling kernel mappings; PR #3657 owns that scope.
- Adding the persistent forced-Svade CI lane; PR3 owns that scope.
- Adding page replacement, swapping, or a new dirty-page writeback policy.
- Changing the VMAR permission or signal-delivery model.
- Optimizing away rare TLB shootdowns before correctness is established.

## Considered Approaches

### Eager A/D for every user mapping

Presetting A/D on all user pages is small, but it destroys the useful access
and dirty status of tracked RAM. It also changes the meaning of existing VM
accounting and future reclamation policies. Rejected.

### Repair A/D directly in the RISC-V trap layer

The trap handler could walk and mutate the active page table before entering
the generic VM path. This duplicates permission and COW decisions, bypasses
the VM-space locking and TLB-coherence model, and exposes raw PTE mutation at
the architecture boundary. Rejected.

### Purpose-aware user-mapping policy

Tracked RAM is repaired lazily by the existing `VmMapping` page-fault path.
Untracked user I/O receives conservative A/D status at the OSTD mapping
boundary because its status is not consumed and its faults are not
recoverable. Selected.

Splitting these two user-mapping cases into separate PRs was also considered.
It would leave PR2 functionally incomplete and either require a fourth PR or
mix runtime behavior into the CI-only PR3, so both cases remain in PR2.

## Architecture

### Keep exception decoding unchanged

The RISC-V conversion from `CpuException` to `PageFaultInfo` already maps:

- instruction faults to `VmPerms::EXEC`;
- load faults to `VmPerms::READ`;
- store faults to `VmPerms::READ | VmPerms::WRITE`.

PR2 derives `PageAccess::Write` when `required_perms` contains
`VmPerms::WRITE`; instruction and load faults use `PageAccess::Read`.
No new exception type or RISC-V-only trap callback is required.

This keeps kernel-space faults on the existing panic path, including the
trapframe and panic backtrace. Failed user-space permission checks continue to
flow through the existing signal or exception-table recovery path.

### Repair tracked user RAM in `VmMapping`

The existing `handle_single_page_fault` path already distinguishes:

- an existing RAM mapping whose PTE permissions satisfy the access;
- an existing read-only mapping that needs COW for a write;
- an absent mapping that needs population;
- a device mapping whose fault is not recoverable.

Only the first case changes. Before returning, it records the required
`PageAccess` through `CursorMut::protect_next`. The generic page-table
compare-exchange added by PR #3657 makes this property update atomic with
respect to concurrent hardware A/D changes.

After updating the PTE, the path uses the cursor-owned `TlbFlusher` to:

1. issue a range flush for the faulting page;
2. dispatch it to every CPU on which the `VmSpace` may be active;
3. wait for completion before returning.

The repair is idempotent. PR2 always uses the complete existing flush sequence
after this rare fault instead of adding a second predicate and a
current-CPU-only optimization.

The same `PageAccess` operation replaces the current hand-written A/D setting
when COW installs writable permissions and when a demand fault creates a new
page. This gives A/D semantics one source without changing those paths'
decisions.

### Preserve fault-around while repairing its target

Read faults on eligible VMO mappings may populate surrounding pages. Existing
populated pages are normally skipped, but the actual faulting page cannot be
skipped when its A bit is clear.

The fault-around traversal therefore treats the target specially:

- an absent target is populated as before;
- an existing target with sufficient PTE permissions records
  `PageAccess::Read` and completes the normal TLB flush;
- other already-populated surrounding pages remain untouched;
- if an earlier surrounding-page operation fails, the existing fallback calls
  the single-page handler for the target rather than returning merely because
  a PTE exists.

This keeps read-ahead policy unchanged while ensuring the faulting instruction
can make progress.

### Prepare user I/O at the OSTD mapping boundary

`VmItem::new_untracked_io` is the point that knows a new user mapping is I/O
rather than tracked RAM. On RISC-V only, it records:

- `PageAccess::Read` for non-writable I/O, producing `A=1, D=0`;
- `PageAccess::Write` for writable I/O, producing `A=1, D=1`.

The stored `PageProperty` is then encoded normally. `UserPtConfig` does not
inject policy in `item_raw_info`.

Raw reconstruction uses a separate private constructor that accepts the
property exactly as encoded. Split, copy, unmap, and restoration therefore do
not silently add status bits.

When `CursorMut::protect_next` changes an untracked I/O mapping, the operation
first applies the caller's permission update and then reapplies the same
RISC-V I/O status policy. The existing private `AVAIL1` marker identifies
untracked I/O; tracked RAM remains untouched.

## Data Flow

For tracked user RAM:

```text
user instruction
  -> RISC-V page-fault exception
  -> PageFaultInfo with required permissions
  -> VMAR permission validation
  -> VmMapping identifies an existing permitted RAM PTE
  -> PageFlags::record_access(PageAccess)
  -> atomic page-table protect
  -> VM-space TLB shootdown and synchronization
  -> return and retry the user instruction
```

For user I/O:

```text
device mapping or protection update
  -> OSTD recognizes an untracked I/O VmItem
  -> PageFlags::record_access(Read or Write)
  -> exact PageProperty-to-PTE encoding
  -> user access proceeds without an Svade A/D fault
```

## File Map

| File | Responsibility |
|---|---|
| `ostd/src/mm/vm_space.rs` | Apply and preserve the RISC-V user-I/O A/D policy while keeping raw restoration exact. |
| `ostd/src/mm/test.rs` | Test tracked-RAM laziness, read-only/writable I/O, protection, and raw-property behavior. |
| `kernel/src/vm/vmar/vm_mapping.rs` | Repair tracked-RAM A/D faults, fault-around targets, and use the shared `PageAccess` operation. |
| `kernel/src/vm/vmar/vmar_impls/mod.rs` | Provide minimal ktest-only `RssDelta` construction for exercising the real fault handler. |
| `kernel/src/vm/vmar/vmar_impls/fork.rs` | Keep existing I/O copy/fork assertions architecture-correct. |

Production behavior changes are confined to one OSTD file and one kernel file.
The other three files contain adjacent regression-test support or assertions.

## Testing Strategy

Implementation follows red-green-refactor. Each new behavior is first
demonstrated by a failing focused test on the PR #3657 baseline.

### Focused OSTD tests

- tracked user RAM preserves caller-supplied clear A/D flags;
- read-only RISC-V user I/O receives `A` but not `D`;
- writable RISC-V user I/O receives `A|D`;
- changing writable I/O to read-only retains `A` and clears `D`;
- raw user-I/O restoration preserves the exact encoded property;
- x86-64 and LoongArch64 retain their existing mapping properties.

### Focused kernel tests

- an existing permitted RAM mapping with clear A/D records only `A` for a
  read or instruction access;
- the same mapping records `A|D` for a write;
- permissions, cache policy, and software-reserved flags remain unchanged;
- the COW path still runs when the PTE lacks write permission;
- an existing fault-around target receives `A`;
- an error while processing an earlier surrounding page falls back to the
  target's single-page handler;
- existing I/O mapping copy/fork behavior preserves the prepared property.

### Build and runtime validation

- `cargo fmt --all -- --check`;
- RISC-V OSTD and kernel cross-compilation in Sv48 and Sv39 modes;
- x86-64 OSTD/kernel checks with its default `cvm_guest` feature;
- LoongArch64 cross-compilation for shared VM regressions;
- focused RISC-V OSTD and kernel tests;
- `make check`;
- full four-hart QEMU runs for:
  - Sv48 with hardware A/D updates;
  - Sv39 with hardware A/D updates;
  - Sv48 with forced Svade;
  - Sv39 with forced Svade.

The forced-Svade runs must reach the same userspace success marker as the
hardware-A/D controls and must retain panic/backtrace output on any unexpected
kernel fault.

## Acceptance Criteria

- Every focused regression test is observed failing for the intended reason
  before its implementation and passing afterward.
- Tracked user RAM retains meaningful lazy A/D state until an access is
  recorded.
- User I/O cannot raise an A/D-only fault under Svade.
- Permission failures and COW decisions are unchanged.
- Raw PTE property restoration remains exact.
- TLB updates use the existing VM-space multi-CPU synchronization path.
- The four-mode, four-hart QEMU matrix reaches userspace successfully.
- Shared x86-64 and LoongArch64 checks pass.
- No critical or major Asterinas persona-review finding remains.
- The final uncommitted diff and all test evidence are reviewed by the user
  before any commit.
