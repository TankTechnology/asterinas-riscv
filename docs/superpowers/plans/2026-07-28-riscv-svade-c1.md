# RISC-V Svade C1: Kernel Mapping A/D Preparation Implementation Plan

> **For agentic workers:** Execute this plan task by task. Stop at every review
> gate and obtain the user's explicit approval before creating the corresponding
> commit.

**Goal:** Make OSTD boot and kernel mappings run correctly when RISC-V hardware
does not update PTE A/D bits automatically, while preserving the existing page
table abstractions and the behavior of x86-64 and LoongArch64.

**Architecture:** Keep architecture PTE encoding exact: `PteTrait::from_repr`
must continue to encode exactly the supplied `PageProperty`. Express a page
access with a shared typed operation, preserve hardware-updated A/D status with
an atomic compare-exchange in the generic page-table layer, and apply the
RISC-V eager-A/D policy only when new private kernel mappings are constructed.
Raw item restoration remains exact. RISC-V assembly boot leaves receive A/D
directly because they exist before the Rust mapping constructors.

**Tech Stack:** Rust `no_std`, Asterinas OSTD page tables, RISC-V Sv39/Sv48,
RISC-V assembly, `ktest`, `cargo-osdk`, QEMU `virt`, Docker.

---

## Scope and invariants

This plan implements only C1 of issue #3589:

- shared typed page-access status update;
- atomic preservation of concurrently updated A/D bits during `protect`;
- eager A/D on newly constructed RISC-V kernel mappings;
- eager A/D on RISC-V early boot and frame-metadata mappings;
- Sv39/Sv48, default-Svadu/forced-Svade, and cross-architecture checks.

It deliberately does **not**:

- change `ostd/src/arch/riscv/mm/mod.rs`;
- inject A/D bits in `PteTrait::from_repr` or `PageTableEntry::set_prop`;
- change user mappings, user I/O, or the page-fault handler;
- add a trap-layer raw-PTE update path;
- add Python or shell helper files;
- change Makefile, `tools/qemu_args.sh`, or CI;
- fix the pre-existing safety-documentation issue in
  `KVirtArea::map_untracked_frames`;
- close issue #3589.

The persistent forced-Svade CI lane belongs to C3. C1 uses the existing
`cargo osdk test --qemu-args=...` override, so no test-infrastructure source
change is needed.

The following contracts must remain true:

1. `from_repr(to_repr(pte))` remains an exact representation round trip.
2. A raw page-table item restored by `item_from_raw` is not treated as a new
   mapping and gains no policy bits.
3. The generic CAS loop may merge only `ACCESSED | DIRTY` from a failed
   comparison. It must never merge permissions, cache policy, physical address,
   PTE kind, or software-reserved flags.
4. Read/execute mappings receive A but not D. Writable mappings receive A and D.
5. Non-RISC-V mapping properties remain bit-for-bit unchanged.
6. RISC-V non-leaf boot PTEs remain `V`-only.

## Worktree and test shell

Work only in:

```text
/home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c1
```

Branch:

```text
codex/riscv-svade-c1
```

The branch was created from `upstream/main` at
`6e57d226d3b3db6d733aaa6ad05cc80c207f96c6`. The clean baseline already passed:

```bash
cargo fmt --all -- --check
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none --no-default-features
```

Runtime tests must use the project container because the host's installed
`cargo-osdk` has a newer glibc requirement. At the start of an execution shell,
define this temporary function; do not add it to the repository:

```bash
run_c1_container() {
  docker run --rm --privileged --network=host \
    -v /dev:/dev \
    -v /home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c1:/root/asterinas \
    -w /root/asterinas \
    asterinas/asterinas:0.18.0-20260702 "$@"
}
```

Before the first runtime test, install the checkout's OSDK into the worktree's
ignored `target/` directory. This survives the short-lived containers without
adding a repository file:

```bash
run_c1_container bash -lc \
  'CARGO_INSTALL_ROOT=/root/asterinas/target/c1-cargo OSDK_LOCAL_DEV=1 cargo install cargo-osdk --path osdk'
```

Expected: `cargo-osdk` installs successfully as
`/root/asterinas/target/c1-cargo/bin/cargo-osdk`.

Every container command that invokes `cargo osdk` below begins by prepending
that directory to `PATH`.

## Review and commit policy

Every task below is one prospective commit. For each task:

1. add and run the failing test first;
2. make the smallest implementation change;
3. run the task's green tests;
4. run `cargo fmt --all -- --check`;
5. run the Asterinas persona-based code review on the uncommitted diff;
6. show the user the exact diff, test output summary, and review findings;
7. stop;
8. only after explicit user approval, stage the listed files and commit.

Do not stage or commit a later task while an earlier review gate is awaiting
approval. Do not push or open the PR until the user separately approves it.

## File map

| Concern | Files |
|---|---|
| Typed access contract | `ostd/src/mm/mod.rs`, `ostd/src/mm/page_prop.rs` |
| Atomic status preservation | `ostd/src/mm/page_table/mod.rs`, `ostd/src/mm/page_table/node/mod.rs`, `ostd/src/mm/page_table/node/entry.rs`, `ostd/src/mm/page_table/test.rs` |
| Private kernel mapping policy | `ostd/src/mm/kspace/mod.rs`, `ostd/src/mm/kspace/kvirt_area.rs`, `ostd/src/mm/kspace/test.rs`, `ostd/src/mm/page_table/test.rs` |
| Early RISC-V mappings | `ostd/src/arch/riscv/boot/bsp_boot.S`, `ostd/src/mm/frame/meta.rs` |

There are eleven distinct files because the typed operation must also be
re-exported across OSTD's public `mm` boundary for C3's separate kernel-crate
fault handler. The remaining files cross four existing
abstraction boundaries and keep each regression test beside the boundary it
protects. No new runtime source module or test helper file is introduced.

---

### Task 1: Add the typed page-access status operation

**Files:**

- Modify: `ostd/src/mm/mod.rs`
- Modify: `ostd/src/mm/page_prop.rs`

- [ ] **Step 1: Add compile-failing contract tests**

Add these tests immediately after the `PageFlags` definition:

```rust
#[cfg(ktest)]
mod tests {
    use super::{PageAccess, PageFlags};
    use crate::prelude::ktest;

    // Regression tests for Asterinas issue #3589.
    #[ktest]
    fn record_access_for_read_preserves_flags_and_sets_accessed() {
        let mut flags = PageFlags::RX | PageFlags::AVAIL2;

        flags.record_access(PageAccess::Read);

        assert_eq!(
            flags,
            PageFlags::RX | PageFlags::AVAIL2 | PageFlags::ACCESSED
        );
    }

    #[ktest]
    fn record_access_for_write_preserves_flags_and_sets_accessed_and_dirty() {
        let mut flags = PageFlags::RW | PageFlags::AVAIL2;

        flags.record_access(PageAccess::Write);

        assert_eq!(
            flags,
            PageFlags::RW | PageFlags::AVAIL2 | PageFlags::ACCESSED | PageFlags::DIRTY
        );
    }
}
```

- [ ] **Step 2: Verify the tests fail for the intended missing API**

Run:

```bash
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
```

Expected: compilation fails because `PageAccess` and
`PageFlags::record_access` do not exist. A different error must be investigated
before continuing.

- [ ] **Step 3: Add the minimal typed implementation**

Add:

```rust
/// The kind of memory access recorded in page status flags.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PageAccess {
    /// A read or instruction fetch.
    Read,
    /// A write.
    Write,
}

impl PageFlags {
    /// Records an access while preserving all existing flags.
    ///
    /// Every access sets [`Self::ACCESSED`]. A write also sets
    /// [`Self::DIRTY`]. Recording a read does not clear an existing dirty bit.
    pub fn record_access(&mut self, access: PageAccess) {
        self.insert(Self::ACCESSED);
        if access == PageAccess::Write {
            self.insert(Self::DIRTY);
        }
    }
}
```

In `ostd/src/mm/mod.rs`, add `PageAccess` to the existing public
`page_prop::{...}` re-export:

```rust
page_prop::{CachePolicy, PageAccess, PageFlags, PageProperty},
```

The enum and method must be public because C3's A/D fault repair lives in the
separate kernel crate. Do not make the `page_prop` module itself public.

- [ ] **Step 4: Run compile and focused runtime tests**

Run:

```bash
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none --no-default-features
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test record_access --scheme riscv --qemu-args="-smp 4"'
cargo fmt --all -- --check
```

Expected: both cross-compiles pass; two `record_access` ktests pass on RISC-V;
format check passes.

- [ ] **Step 5: Review gate**

Review for:

- `no-bool-args`: the API uses `PageAccess`, not a Boolean;
- preservation of existing flags, including a previously dirty page;
- no architecture-specific condition in the shared primitive;
- the narrow public re-export needed by the later kernel fault path.

Show the uncommitted diff and results to the user, then stop.

- [ ] **Step 6: Commit only after explicit approval**

```bash
git add ostd/src/mm/mod.rs ostd/src/mm/page_prop.rs
git commit -m "Add typed page access status operation"
```

---

### Task 2: Preserve concurrent hardware A/D updates during protection

**Files:**

- Modify: `ostd/src/mm/page_table/mod.rs`
- Modify: `ostd/src/mm/page_table/node/mod.rs`
- Modify: `ostd/src/mm/page_table/node/entry.rs`
- Modify: `ostd/src/mm/page_table/test.rs`

- [ ] **Step 1: Add the failing race regression test**

In `ostd/src/mm/page_table/test.rs`, add `self` to the existing `crate::mm`
import so the test can call a qualified free function. Inside
`mod protection_and_query`, add:

```rust
/// Returns the leaf PTE pointer for a mapped virtual address.
///
/// # Safety
///
/// `root_paddr` must identify a live `C` page table whose traversed nodes
/// remain valid and atomically accessed. The returned pointer must not outlive
/// or be used after removing its leaf page-table node.
unsafe fn mapped_pte_ptr<C: PageTableConfig>(
    root_paddr: Paddr,
    vaddr: Vaddr,
) -> (*mut C::E, PagingLevel) {
    let mut node_vaddr = mm::paddr_to_vaddr(root_paddr);

    for level in (1..=C::NR_LEVELS).rev() {
        // SAFETY: The caller guarantees that `root_paddr` owns a live page
        // table and that the calculated index is in bounds.
        let pte_ptr =
            unsafe { (node_vaddr as *mut C::E).add(pte_index::<C>(vaddr, level)) };
        // SAFETY: The page-table node is alive and all PTE accesses are atomic.
        let pte = unsafe { load_pte(pte_ptr, Ordering::Acquire) };
        match pte.to_repr(level) {
            PteScalar::PageTable(next_paddr, _) => {
                node_vaddr = mm::paddr_to_vaddr(next_paddr);
            }
            PteScalar::Mapped(_, _) => return (pte_ptr, level),
            PteScalar::Absent => panic!("mapping is absent"),
        }
    }

    panic!("mapping has no leaf PTE")
}
```

Then add:

```rust
// Regression test for Asterinas issue #3589.
#[ktest]
fn protect_preserves_concurrent_hardware_status_update() {
    let page_table = PageTable::<TestPtConfig>::empty();
    let range = PAGE_SIZE..PAGE_SIZE * 2;
    let prop = PageProperty::new_user(PageFlags::RW, CachePolicy::Writeback);
    map_untracked(&page_table, range.clone(), 0, prop);

    // SAFETY: `page_table` owns a live mapping covering `range.start`.
    let (pte_ptr, level) =
        unsafe { mapped_pte_ptr::<TestPtConfig>(page_table.root_paddr(), range.start) };
    let preempt_guard = disable_preempt();
    let mut cursor = page_table.cursor_mut(&preempt_guard, &range).unwrap();
    let mut simulate_hardware_update_fn = |prop: &mut PageProperty| {
        // Simulate the MMU setting DIRTY after the cursor's initial PTE load.
        // SAFETY: The pointer remains a live, aligned leaf PTE and all accesses
        // use the page-table atomic helpers.
        let hardware_pte = unsafe { load_pte(pte_ptr, Ordering::Acquire) };
        let PteScalar::Mapped(paddr, mut hardware_prop) = hardware_pte.to_repr(level)
        else {
            panic!("leaf PTE stopped mapping a page");
        };
        hardware_prop.flags |= PageFlags::DIRTY;
        let hardware_pte =
            PageTableEntry::from_repr(&PteScalar::Mapped(paddr, hardware_prop), level);
        // SAFETY: The replacement changes only the hardware-managed DIRTY bit.
        unsafe { store_pte(pte_ptr, hardware_pte, Ordering::Release) };

        prop.flags |= PageFlags::ACCESSED;
    };
    // SAFETY: The operation changes only public status flags and does not alter
    // the mapping identity or its software-reserved ownership bit.
    let protected =
        unsafe { cursor.protect_next(range.len(), &mut simulate_hardware_update_fn) };
    assert_eq!(protected, Some(range.clone()));
    drop(cursor);

    let (_, updated_prop) = page_table.page_walk(range.start).unwrap();
    assert_eq!(
        updated_prop.flags,
        PageFlags::RW | PageFlags::ACCESSED | PageFlags::DIRTY
    );
}
```

- [ ] **Step 2: Verify the regression test exposes the lost update**

Run:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test protect_preserves_concurrent_hardware_status_update --scheme riscv --qemu-args="-smp 4"'
```

Expected: the assertion fails because the current unconditional store loses
the simulated `DIRTY` update. If it does not fail, stop and inspect the test
before changing production code.

- [ ] **Step 3: Add one generic atomic compare-exchange helper**

In `ostd/src/mm/page_table/mod.rs`, beside `load_pte` and `store_pte`, add:

```rust
/// Atomically replaces a page table entry if it still matches `current`.
///
/// # Safety
///
/// The safety preconditions are the same as those of
/// [`AtomicUsize::from_ptr`].
pub(in crate::mm::page_table) unsafe fn compare_exchange_pte<E: PteTrait>(
    ptr: *mut E,
    current: E,
    new: E,
    success: Ordering,
    failure: Ordering,
) -> Result<E, E> {
    // SAFETY: The caller upholds `AtomicUsize::from_ptr`'s requirements.
    let atomic = unsafe { AtomicUsize::from_ptr(ptr.cast()) };
    atomic
        .compare_exchange(current.as_usize(), new.as_usize(), success, failure)
        .map(E::from_usize)
        .map_err(E::from_usize)
}
```

Also correct the nearby load/store safety prose from “are same as” to “are the
same as”; do not otherwise refactor those helpers.

- [ ] **Step 4: Expose the operation only through the locked node guard**

In `ostd/src/mm/page_table/node/mod.rs`, add
`page_table::{self, ...}` to the existing import and add this method beside
`write_pte`:

```rust
/// Replaces a page table entry if it has not changed since it was read.
///
/// # Safety
///
/// The caller must uphold the same requirements as [`Self::write_pte`] for
/// `new_pte`.
pub(super) unsafe fn compare_exchange_pte(
    &mut self,
    idx: usize,
    current: C::E,
    new_pte: C::E,
) -> Result<C::E, C::E> {
    debug_assert!(idx < nr_subpage_per_huge::<C>());
    let ptr = paddr_to_vaddr(self.paddr()) as *mut C::E;
    // SAFETY: The node is alive, the index is in bounds, and all accesses are
    // atomic. The caller validates the replacement PTE.
    unsafe {
        page_table::compare_exchange_pte(
            ptr.add(idx),
            current,
            new_pte,
            Ordering::Release,
            Ordering::Relaxed,
        )
    }
}
```

- [ ] **Step 5: Replace `Entry::protect`'s store with a narrow CAS loop**

Import `PageFlags`, retain the property's result from `op`, and retry only if
the PTE changed. On a failed CAS:

```rust
let PteScalar::Mapped(actual_pa, actual_prop) = actual_pte.to_repr(level) else {
    unreachable!("hardware status updates cannot change the PTE kind");
};
debug_assert_eq!(actual_pa, pa);
let hardware_status =
    actual_prop.flags & (PageFlags::ACCESSED | PageFlags::DIRTY);
new_prop.flags |= hardware_status;
self.pte = actual_pte;
```

The full commit path must be:

```rust
loop {
    let new_pte = C::E::from_repr(&PteScalar::Mapped(pa, new_prop), level);
    // SAFETY: The replacement differs only in PageProperty and keeps the same
    // child, address, configuration, and paging level.
    match unsafe { self.node.compare_exchange_pte(self.idx, self.pte, new_pte) } {
        Ok(_) => {
            self.pte = new_pte;
            return;
        }
        Err(actual_pte) => {
            // Merge only hardware-managed A/D status as shown above.
        }
    }
}
```

Do not call `op` again after a retry. Do not merge `actual_prop` wholesale.

- [ ] **Step 6: Run focused and cross-architecture verification**

Run:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test protect_preserves_concurrent_hardware_status_update --scheme riscv --qemu-args="-smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test protection_and_query --scheme riscv --qemu-args="-smp 4"'
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none --no-default-features
cargo fmt --all -- --check
```

Expected: the race regression and existing protection/query tests pass; both
architectures compile.

- [ ] **Step 7: Review gate**

Review for:

- atomic access on every use of the test PTE pointer;
- safety comments that match the actual lifetime and locking;
- `Release` success and `Relaxed` failure ordering consistency with the
  existing `write_pte`;
- no retry of an arbitrary caller closure;
- only A/D merged after a failed comparison;
- unchanged mapping identity and ownership semantics.

Show the uncommitted diff and results to the user, then stop.

- [ ] **Step 8: Commit only after explicit approval**

```bash
git add \
  ostd/src/mm/page_table/mod.rs \
  ostd/src/mm/page_table/node/mod.rs \
  ostd/src/mm/page_table/node/entry.rs \
  ostd/src/mm/page_table/test.rs
git commit -m "Preserve concurrent page-table status updates"
```

---

### Task 3: Apply eager A/D only at new RISC-V kernel mapping construction

**Files:**

- Modify: `ostd/src/mm/kspace/mod.rs`
- Modify: `ostd/src/mm/kspace/kvirt_area.rs`
- Modify: `ostd/src/mm/kspace/test.rs`
- Modify: `ostd/src/mm/page_table/test.rs`

- [ ] **Step 1: Add failing policy and round-trip tests**

In `ostd/src/mm/kspace/test.rs`, import `PageAccess` beside `PageFlags`, then
add:

```rust
fn read_only_prop() -> PageProperty {
    PageProperty::new_user(PageFlags::RX, CachePolicy::Writeback)
}

fn expected_mapped_prop(prop: PageProperty, access: PageAccess) -> PageProperty {
    #[cfg(target_arch = "riscv64")]
    {
        let mut prop = prop;
        prop.flags.record_access(access);
        prop
    }

    #[cfg(not(target_arch = "riscv64"))]
    {
        let _ = access;
        prop
    }
}
```

Update the existing tracked and untracked `KVirtArea` query assertions so that:

- writable RISC-V mappings expect `A | D`;
- the same non-RISC-V mappings expect the original flags.

Add:

```rust
// Regression test for Asterinas issue #3589.
#[ktest]
fn kvirt_area_untracked_read_only_map_page() {
    let max_paddr = max_paddr();
    let pa_range = max_paddr..max_paddr + PAGE_SIZE as Paddr;
    // SAFETY: The range starts beyond tracked physical memory and the test only
    // queries the mapping; it never dereferences the untracked address.
    let kvirt_area = unsafe {
        KVirtArea::map_untracked_frames(
            PAGE_SIZE,
            0,
            pa_range.clone(),
            read_only_prop(),
        )
    };

    let guard = disable_preempt();
    let MappedItemRef::Untracked(pa, level, prop) =
        kvirt_area.query(&guard, kvirt_area.start()).unwrap()
    else {
        panic!("expected an untracked page");
    };

    assert_eq!(pa, pa_range.start);
    assert_eq!(level, 1);
    assert_eq!(
        prop,
        expected_mapped_prop(read_only_prop(), PageAccess::Read)
    );
    assert!(!prop.flags.contains(PageFlags::DIRTY));
}
```

Add an exact raw-restoration regression:

```rust
// Regression test for Asterinas issue #3589.
#[ktest]
fn kernel_pt_raw_info_preserves_status_flags() {
    let prop = default_prop();
    // SAFETY: AVAIL1 is clear, so this restores an untracked scalar item and
    // takes no ownership of the test physical address.
    let item = unsafe { KernelPtConfig::item_from_raw(max_paddr(), 1, prop) };

    let (_, _, raw_prop) = KernelPtConfig::item_raw_info(&item);

    assert_eq!(raw_prop.flags, prop.flags);
}
```

This test intentionally expects no A/D insertion on raw restoration.

- [ ] **Step 2: Verify the new-mapping tests fail for the intended reason**

Run:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test kvirt_area --scheme riscv --qemu-args="-smp 4"'
```

Expected: the RISC-V new-mapping assertions fail because mappings do not yet
contain eager status bits; the raw-restoration assertion passes.

- [ ] **Step 3: Make direct construction of a new mapped item impossible**

In `ostd/src/mm/kspace/mod.rs`, replace the public-in-module enum with:

```rust
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct MappedItem(MappedItemKind);

#[derive(Clone, Debug, Eq, PartialEq)]
enum MappedItemKind {
    Tracked(Frame<dyn AnyFrameMeta>, PageProperty),
    Untracked(Paddr, PagingLevel, PageProperty),
}
```

Add `PageAccess` to the existing private `page_prop::{...}` import in this
module.

Add:

```rust
impl MappedItem {
    fn tracked(frame: Frame<dyn AnyFrameMeta>, prop: PageProperty) -> Self {
        Self(MappedItemKind::Tracked(
            frame,
            prepare_new_mapping_prop(prop),
        ))
    }

    pub(super) fn untracked(
        paddr: Paddr,
        level: PagingLevel,
        prop: PageProperty,
    ) -> Self {
        Self(MappedItemKind::Untracked(
            paddr,
            level,
            prepare_new_mapping_prop(prop),
        ))
    }
}

fn prepare_new_mapping_prop(prop: PageProperty) -> PageProperty {
    #[cfg(target_arch = "riscv64")]
    {
        let mut prop = prop;
        let access = if prop.flags.contains(PageFlags::W) {
            PageAccess::Write
        } else {
            PageAccess::Read
        };
        prop.flags.record_access(access);
        prop
    }
    #[cfg(not(target_arch = "riscv64"))]
    {
        prop
    }
}
```

`MappedItem::tracked` and `MappedItem::untracked` are the only new-mapping
policy entrances.

- [ ] **Step 4: Keep raw restoration exact**

Update `KernelPtConfig::item_raw_info` to match `&item.0`.

Update `item_from_raw` to construct `MappedItem(MappedItemKind::...)`
directly. It must **not** call `MappedItem::tracked`,
`MappedItem::untracked`, or `prepare_new_mapping_prop`.

`item_ref_from_raw` continues to return `MappedItemRef` and needs no policy
change.

- [ ] **Step 5: Route every private kernel mapping call site through the constructors**

Replace direct variants in:

- the three mapping loops in `ostd/src/mm/kspace/mod.rs`;
- tracked and untracked mappings in
  `ostd/src/mm/kspace/kvirt_area.rs`;
- `jump_near_address_space_end` in
  `ostd/src/mm/page_table/test.rs`.

Use only:

```rust
MappedItem::tracked(frame, prop)
MappedItem::untracked(paddr, level, prop)
```

Do not add policy to `MappedItemRef`, cursor code, architecture PTE encoding,
or query code.

- [ ] **Step 6: Run focused tests and exact cross-architecture checks**

Run:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test kvirt_area --scheme riscv --qemu-args="-smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test kernel_pt_raw_info_preserves_status_flags --scheme riscv --qemu-args="-smp 4"'
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target loongarch64-unknown-none --no-default-features
cargo fmt --all -- --check
```

Expected:

- writable RISC-V kernel mappings contain A/D;
- read-only RISC-V mappings contain A but not D;
- raw restoration is exact;
- x86-64 and LoongArch64 compile with unchanged policy;
- formatting passes.

If the LoongArch target dependency is unavailable outside the project image,
rerun that command through `run_c1_container`; do not weaken or omit the check.

- [ ] **Step 7: Review gate**

Review for:

- policy only on new item construction;
- no constructor bypass remains (`rg 'MappedItem::(Tracked|Untracked)' ostd`);
- raw restoration remains exact;
- RISC-V-only semantic change;
- read-only mappings never gain D;
- no extra public API and no trap/PTE-encoder shortcut.

Show the uncommitted diff and results to the user, then stop.

- [ ] **Step 8: Commit only after explicit approval**

```bash
git add \
  ostd/src/mm/kspace/mod.rs \
  ostd/src/mm/kspace/kvirt_area.rs \
  ostd/src/mm/kspace/test.rs \
  ostd/src/mm/page_table/test.rs
git commit -m "Preset RISC-V kernel mapping A/D bits"
```

---

### Task 4: Initialize early RISC-V leaf mappings with A/D

**Files:**

- Modify: `ostd/src/arch/riscv/boot/bsp_boot.S`
- Modify: `ostd/src/mm/frame/meta.rs`

- [ ] **Step 1: Record the system-level red test before implementation**

Run the current C1 branch under forced Svade:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --qemu-args="-cpu rv64,svpbmt=true,zkr=true,svadu=false,svade=true -smp 4"'
```

Expected before this task: boot does not reach successful OSTD ktest
completion because an early writable leaf has clear A/D status. Save the first
fault or hang point in the review evidence. A QEMU option error is an
environment failure, not the expected red result, and must be fixed before
continuing.

- [ ] **Step 2: Set A/D on RISC-V assembly leaf entries only**

In `ostd/src/arch/riscv/boot/bsp_boot.S`, define:

```asm
PTE_A                  = 0x40
PTE_D                  = 0x80
```

Rename the leaf constant to:

```asm
PTE_VRWXAD = PTE_V | PTE_R | PTE_W | PTE_X | PTE_A | PTE_D
```

Use `PTE_VRWXAD` for all identity, linear, and kernel-code leaf entries in both
Sv48 and Sv39 boot tables.

Keep the dynamically written Sv48 non-leaf pointer:

```asm
ori t0, t0, PTE_V
```

and all zero/non-leaf entries unchanged.

- [ ] **Step 3: Set A/D only for RISC-V frame-metadata leaves**

In `ostd/src/mm/frame/meta.rs`, construct the metadata mapping flags as:

```rust
let flags = PageFlags::RW;
#[cfg(target_arch = "riscv64")]
let flags = {
    let mut flags = flags;
    flags.record_access(PageAccess::Write);
    flags
};
let prop = PageProperty {
    flags,
    cache: CachePolicy::Writeback,
    priv_flags: PrivilegedPageFlags::GLOBAL,
};
```

Import `PageAccess` from the private `page_prop` module only under
`target_arch = "riscv64"` so other architectures do not receive an unused
import.

Do **not** change the separate `#[cfg(target_arch = "x86_64")]`
`add_temp_linear_mapping` property. It must remain `PageFlags::RW`. This is a
deliberate compatibility improvement over the historical patch: the x86-only
temporary path has no Svade requirement and should not acquire a semantic
change.

- [ ] **Step 4: Run forced-Svade Sv48 and Sv39 OSTD tests**

Run:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --qemu-args="-cpu rv64,svpbmt=true,zkr=true,svadu=false,svade=true -smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --features riscv_sv39_mode --qemu-args="-cpu rv64,svpbmt=true,zkr=true,svadu=false,svade=true -smp 4"'
```

Expected: both full OSTD ktest runs boot and pass under forced Svade with four
harts.

- [ ] **Step 5: Run default-hardware-update and cross-architecture checks**

Run:

```bash
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --qemu-args="-cpu rv64,svpbmt=true,zkr=true -smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --features riscv_sv39_mode --qemu-args="-cpu rv64,svpbmt=true,zkr=true -smp 4"'
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target loongarch64-unknown-none --no-default-features
cargo fmt --all -- --check
```

Expected: Sv48 and Sv39 still pass when hardware updates A/D; non-RISC-V
targets compile; formatting passes.

- [ ] **Step 6: Review gate**

Review for:

- all assembly leaf entries have A/D;
- no non-leaf entry has R/W/X/A/D;
- both Sv39 and Sv48 tables are covered;
- frame metadata uses the shared typed operation;
- the x86-only temporary mapping is unchanged;
- no other boot, image, or board-support behavior changes.

Show the uncommitted diff and results to the user, then stop.

- [ ] **Step 7: Commit only after explicit approval**

```bash
git add \
  ostd/src/arch/riscv/boot/bsp_boot.S \
  ostd/src/mm/frame/meta.rs
git commit -m "Initialize RISC-V boot mapping A/D bits"
```

---

### Task 5: Validate and prepare C1 for the user's final review

**Files:**

- No functional source changes expected.
- Update this plan only if an executed command or result needs correction.

- [ ] **Step 1: Inspect scope and constructor coverage**

Run:

```bash
git status --short
git diff upstream/main...HEAD --stat
git diff upstream/main...HEAD --check
rg -n 'MappedItem::(Tracked|Untracked)' ostd
git diff upstream/main...HEAD -- \
  ostd/src/arch/riscv/mm/mod.rs \
  kernel \
  tools \
  Makefile \
  .github
```

Expected:

- no uncommitted functional files;
- no whitespace errors;
- no direct mapped-item variants;
- no diff in the RISC-V PTE encoder, kernel, tools, Makefile, or CI.

- [ ] **Step 2: Run the final C1 test matrix**

Run:

```bash
cargo fmt --all -- --check
run_c1_container bash -lc 'make check'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --qemu-args="-cpu rv64,svpbmt=true,zkr=true -smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --features riscv_sv39_mode --qemu-args="-cpu rv64,svpbmt=true,zkr=true -smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --qemu-args="-cpu rv64,svpbmt=true,zkr=true,svadu=false,svade=true -smp 4"'
run_c1_container bash -lc \
  'export PATH=/root/asterinas/target/c1-cargo/bin:$PATH; cd /root/asterinas/ostd && cargo osdk test --scheme riscv --features riscv_sv39_mode --qemu-args="-cpu rv64,svpbmt=true,zkr=true,svadu=false,svade=true -smp 4"'
run_c1_container bash -lc \
  "RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' cargo check -p ostd --target x86_64-unknown-none --no-default-features"
run_c1_container bash -lc \
  "RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' cargo check -p ostd --target loongarch64-unknown-none --no-default-features"
```

Expected:

| Architecture/mode | Expected result |
|---|---|
| RISC-V Sv48, default A/D update, 4 harts | all OSTD ktests pass |
| RISC-V Sv39, default A/D update, 4 harts | all OSTD ktests pass |
| RISC-V Sv48, forced Svade, 4 harts | all OSTD ktests pass |
| RISC-V Sv39, forced Svade, 4 harts | all OSTD ktests pass |
| x86-64 `cfg(ktest)` | OSTD compiles |
| LoongArch64 `cfg(ktest)` | OSTD compiles |
| repository checks | `make check` passes |

- [ ] **Step 3: Perform final Asterinas code review**

Run the `aster-code-review` skill in diff mode against `upstream/main`.
Review with these personas:

- Project maintainer: constructor boundary, visibility, naming, focused diff;
- Kernel developer: CAS retry semantics and property preservation;
- Security expert: unsafe PTE test helper and atomic pointer access;
- Hardware expert: Svade leaf/non-leaf contract and Sv39/Sv48 coverage.

Any major or critical finding blocks handoff. Fixes must repeat the relevant
task's test and user-review gate. Do not fold an unrelated cleanup into C1.

- [ ] **Step 4: Present the final review package and stop**

Provide the user:

- four commit hashes and subjects;
- `git diff upstream/main...HEAD --stat`;
- the exact test matrix with pass/fail evidence;
- all review findings, including resolved findings;
- the explicit statement that C1 does not yet support user A/D faults and does
  not close #3589;
- the remaining C2 and C3 work.

Do not push or create a PR until the user explicitly approves the complete C1
branch.
