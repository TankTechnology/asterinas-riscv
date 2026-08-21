# RISC-V Svade User-Mapping A/D Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Svade support for user mappings by preparing untracked user
I/O PTEs and repairing tracked user-RAM A/D faults through the existing VM
fault and TLB-coherence paths.

**Architecture:** Apply eager A/D only at the OSTD boundary that constructs or
protects an untracked user-I/O item. Keep tracked RAM lazy and repair it in
`VmMapping` with the typed `PageAccess` operation, PR #3657's atomic
page-table protection, and the cursor-owned multi-CPU `TlbFlusher`.

**Tech Stack:** Rust `no_std`, Asterinas OSTD and kernel VM, `ktest`,
`cargo-osdk`, RISC-V Sv39/Sv48, QEMU `virt`, Docker.

---

## Scope and review gate

Work only in:

```text
/home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c2
```

Branch:

```text
codex/riscv-svade-c2
```

The branch is stacked on PR #3657 (`codex/riscv-svade-c1`). Do not stage, commit, push,
or open a PR. After implementation, testing, and Asterinas review, present the
complete uncommitted diff to the user.

Production changes are limited to:

- `ostd/src/mm/vm_space.rs`;
- `kernel/src/vm/vmar/vm_mapping.rs`.

Adjacent tests and test support may also modify:

- `ostd/src/mm/test.rs`;
- `kernel/src/vm/vmar/vmar_impls/fork.rs`;
- `kernel/src/vm/vmar/vmar_impls/mod.rs`.

No trap code, architecture PTE encoder, Makefile, workflow, or helper script is
changed.

## Test environment

Install the checkout's OSDK into the ignored worktree target directory:

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v /home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c2:/root/asterinas \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'CARGO_INSTALL_ROOT=/root/asterinas/target/c2-cargo OSDK_LOCAL_DEV=1 cargo install cargo-osdk --path osdk'
```

Every container command that uses `cargo osdk` prepends:

```bash
export PATH=/root/asterinas/target/c2-cargo/bin:$PATH
```

For RISC-V tests, set `SMP=4` in the container environment. Do not rely on a
second `-smp` argument because the RISC-V scheme already supplies one from the
environment.

---

### Task 1: Preset A/D for untracked user I/O

**Files:**

- Modify: `ostd/src/mm/test.rs`
- Modify: `kernel/src/vm/vmar/vmar_impls/fork.rs`
- Modify: `ostd/src/mm/vm_space.rs`

- [ ] **Step 1: Add failing user-I/O behavior tests**

In `ostd/src/mm/test.rs`, add a local helper beside `IOMEM_PADDR`:

```rust
fn expected_iomem_flags(mut flags: PageFlags) -> PageFlags {
    #[cfg(target_arch = "riscv64")]
    flags.record_access(if flags.contains(PageFlags::W) {
        PageAccess::Write
    } else {
        PageAccess::Read
    });
    flags
}
```

Import `PageAccess`, update existing I/O assertions to use the helper, and add
two tests:

```rust
// Regression test for Asterinas issue #3589.
#[ktest]
fn vmspace_map_query_read_only_iomem() {
    let vmspace = VmSpace::new();
    let range = 0x4000..0x5000;
    let iomem = IoMem::acquire(IOMEM_PADDR + 0x4000..IOMEM_PADDR + 0x5000).unwrap();
    let prop = PageProperty::new_user(PageFlags::R, CachePolicy::Uncacheable);
    let preempt_guard = disable_preempt();

    vmspace
        .cursor_mut(&preempt_guard, &range)
        .unwrap()
        .map_iomem(iomem, prop, PAGE_SIZE, 0);

    let mut cursor = vmspace.cursor(&preempt_guard, &range).unwrap();
    let (_, Some(VmQueriedItem::MappedIoMem { prop, .. })) = cursor.query().unwrap() else {
        panic!("query did not return the I/O mapping");
    };
    assert_eq!(prop.flags, expected_iomem_flags(PageFlags::R));
    assert!(!prop.flags.contains(PageFlags::DIRTY));
}

// Regression test for Asterinas issue #3589.
#[ktest]
fn vmspace_protect_iomem_preserves_riscv_status() {
    let vmspace = VmSpace::new();
    let range = 0x5000..0x6000;
    let iomem = IoMem::acquire(IOMEM_PADDR + 0x5000..IOMEM_PADDR + 0x6000).unwrap();
    let prop = PageProperty::new_user(PageFlags::RW, CachePolicy::Uncacheable);
    let preempt_guard = disable_preempt();

    vmspace
        .cursor_mut(&preempt_guard, &range)
        .unwrap()
        .map_iomem(iomem, prop, PAGE_SIZE, 0);
    let mut cursor = vmspace.cursor_mut(&preempt_guard, &range).unwrap();
    assert_eq!(
        cursor.protect_next(PAGE_SIZE, |flags, _| *flags = PageFlags::R),
        Some(range.clone())
    );
    drop(cursor);

    let mut cursor = vmspace.cursor(&preempt_guard, &range).unwrap();
    let (_, Some(VmQueriedItem::MappedIoMem { prop, .. })) = cursor.query().unwrap() else {
        panic!("query did not return the protected I/O mapping");
    };
    assert_eq!(prop.flags, expected_iomem_flags(PageFlags::R));
    assert!(!prop.flags.contains(PageFlags::DIRTY));
}
```

In the `cow_copy_pt_iomem` test, add the same architecture-aware expected
property calculation and use it for the initial, child, and sibling I/O
assertions.

- [ ] **Step 2: Add failing raw-restoration tests**

At the end of `ostd/src/mm/vm_space.rs`, add a `#[cfg(ktest)]` test module
that checks:

```rust
#[ktest]
fn user_io_raw_info_preserves_status_flags() {
    let prop = PageProperty::new_user(PageFlags::RW, CachePolicy::Uncacheable);
    let item = VmItem::new_untracked_io(PAGE_SIZE, prop);
    let (_, _, raw_prop) = UserPtConfig::item_raw_info(&item);
    assert_eq!(raw_prop.flags, item.prop.flags);
}

#[cfg(target_arch = "riscv64")]
#[ktest]
fn user_io_raw_restoration_preserves_exact_property() {
    let mut prop =
        PageProperty::new_user(PageFlags::RW | PageFlags::AVAIL2, CachePolicy::Uncacheable);
    prop.priv_flags |= PrivilegedPageFlags::AVAIL1;

    // SAFETY: The aligned address and level describe an untracked I/O item.
    let restored = unsafe { UserPtConfig::item_from_raw(PAGE_SIZE, 1, prop) };

    assert_eq!(restored.prop, prop);
}
```

- [ ] **Step 3: Run RED tests**

Run the RISC-V OSTD and kernel filters:

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v /home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c2:/root/asterinas \
  -w /root/asterinas asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'export PATH=/root/asterinas/target/c2-cargo/bin:$PATH; export SMP=4; cd ostd; cargo osdk test vmspace_ --scheme riscv'

docker run --rm --privileged --network=host -v /dev:/dev \
  -v /home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c2:/root/asterinas \
  -w /root/asterinas asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'export PATH=/root/asterinas/target/c2-cargo/bin:$PATH; export SMP=4; cargo osdk test cow_copy_pt_iomem --scheme riscv'
```

Expected: the new RISC-V I/O behavior assertions fail because the properties
still lack `ACCESSED|DIRTY`. The raw-restoration assertion passes on the
baseline; it is a preservation test that must remain green while the safe
constructor gains policy.

- [ ] **Step 4: Implement the minimal user-I/O policy**

Import `PageAccess` in `ostd/src/mm/vm_space.rs`. In
`VmItem::new_untracked_io`, record `PageAccess::Write` for writable mappings
and `PageAccess::Read` otherwise, only on RISC-V. Delegate storage to:

```rust
fn from_untracked_io_raw_parts(
    paddr: Paddr,
    level: PagingLevel,
    prop: PageProperty,
) -> Self {
    Self {
        prop,
        mapped_item: MappedItem::UntrackedIoMem { paddr, level },
    }
}
```

Use `from_untracked_io_raw_parts` from `UserPtConfig::item_from_raw`.

After the caller operation in `CursorMut::protect_next`, reapply the policy
only when the RISC-V property contains the private `AVAIL1` I/O marker:

```rust
#[cfg(target_arch = "riscv64")]
if prop.priv_flags.contains(PrivilegedPageFlags::AVAIL1) {
    let access = if prop.flags.contains(PageFlags::W) {
        PageAccess::Write
    } else {
        PageAccess::Read
    };
    prop.flags.record_access(access);
}
```

- [ ] **Step 5: Run GREEN and cross-architecture checks**

Rerun both filters, then:

```bash
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none
cargo fmt --all -- --check
```

Expected: all commands exit zero. RISC-V I/O has the prepared status; x86-64
behavior is unchanged.

---

### Task 2: Repair tracked user-RAM A/D faults

**Files:**

- Modify: `kernel/src/vm/vmar/vmar_impls/mod.rs`
- Modify: `kernel/src/vm/vmar/vm_mapping.rs`

- [ ] **Step 1: Add ktest-only RSS construction**

Keep the production `RssDelta` field unchanged under `not(ktest)`. Under
`ktest`, store `Option<&Vmar>`, add `RssDelta::new_for_test`, and update `Drop`
to update counters only when the option is present.

- [ ] **Step 2: Add failing read/write handler tests**

At the end of `vm_mapping.rs`, add a `#[cfg(ktest)]` module. Create a
`VmSpace`, map a real frame with clear A/D and `AVAIL2`, construct a matching
anonymous `VmMapping`, and call the real `handle_page_fault` with
`RssDelta::new_for_test`.

The read test expects:

```rust
PageFlags::RX | PageFlags::AVAIL2 | PageFlags::ACCESSED
```

and explicitly rejects `DIRTY`. The write test expects:

```rust
PageFlags::RW | PageFlags::AVAIL2 | PageFlags::ACCESSED | PageFlags::DIRTY
```

Both tests verify `CachePolicy::Writeback` is unchanged and exercise the
mapped memory after repair.

- [ ] **Step 3: Add failing fault-around tests**

Add one existing-target test using `VmoOptions::new_anon`, and one fallback
test using a `FailingPageCacheBackend` whose async read/write methods return
`Errno::EIO`. Both call the real handler and assert that the actual target
receives `ACCESSED` but not `DIRTY`.

- [ ] **Step 4: Run RED kernel tests**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v /home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c2:/root/asterinas \
  -w /root/asterinas asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'export PATH=/root/asterinas/target/c2-cargo/bin:$PATH; export SMP=4; cargo osdk test page_fault_handler_ --scheme riscv'
```

Expected: all new tests fail because the existing permitted-mapping and
fault-around paths return without recording the required status.

- [ ] **Step 5: Implement one typed repair helper**

Import `PageAccess` and `vm_space::CursorMut`. Add:

```rust
fn record_page_access(
    cursor: &mut CursorMut<'_>,
    va: Range<Vaddr>,
    access: PageAccess,
) {
    let protected_range = cursor.protect_next(PAGE_SIZE, |flags, _| {
        flags.record_access(access);
    });
    debug_assert_eq!(protected_range, Some(va.clone()));

    cursor
        .flusher()
        .issue_tlb_flush(TlbFlushOp::for_range(va));
    cursor.flusher().dispatch_tlb_flush();
    cursor.flusher().sync_tlb_flush();
}
```

Derive `PageAccess` once from `required_perms`. In the existing permitted RAM
case, call this helper before returning. Use the same typed operation when
forming COW flags, new-page flags, and fault-around read-ahead flags.

In fault-around, repair only the actual existing target. Continue skipping
other populated pages. If the surrounding operation returns an error, call
`handle_single_page_fault` for the target unconditionally.

- [ ] **Step 6: Run GREEN tests and focused regressions**

Rerun the `page_fault_handler_` filter, then run:

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v /home/ubuntu/.config/superpowers/worktrees/asterinas/riscv-svade-c2:/root/asterinas \
  -w /root/asterinas asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'export PATH=/root/asterinas/target/c2-cargo/bin:$PATH; export SMP=4; cargo osdk test cow_copy_pt --scheme riscv'
```

Expected: all focused tests pass.

---

### Task 3: Full validation and review

**Files:** No new production files.

- [ ] **Step 1: Run static and cross-architecture checks**

```bash
cargo fmt --all -- --check
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target riscv64imac-unknown-none-elf --no-default-features
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target x86_64-unknown-none
RUSTFLAGS='--cfg ktest --check-cfg cfg(ktest)' \
  cargo check -p ostd --target loongarch64-unknown-none-softfloat --no-default-features
```

Run `make check` inside the project container. Expected: all exit zero.

- [ ] **Step 2: Run the four-mode OSTD matrix**

Run full `cargo osdk test` with `SMP=4` for:

1. Sv48 default hardware A/D;
2. Sv39 default hardware A/D (`--features riscv_sv39_mode`);
3. Sv48 forced Svade
   (`--qemu-args="-cpu rv64,svpbmt=true,zkr=true,svadu=false,svade=true"`);
4. Sv39 forced Svade with the same QEMU CPU option.

Expected: every OSTD test passes in every mode and the actual QEMU process has
four harts.

- [ ] **Step 3: Run the four-mode userspace boot matrix**

Build the initramfs once, then run the kernel with the existing
`/test/boot_hello.sh` init command in the same four modes and `SMP=4`.
Expected: `qemu.log` contains `Successfully booted.` for every mode.

- [ ] **Step 4: Inspect panic diagnostics**

Search all matrix logs for unexpected instruction/load/store page faults,
kernel panics, and truncated backtraces. Expected: none on successful runs;
the trap and panic implementation is unchanged in the diff.

- [ ] **Step 5: Run Asterinas persona review**

Use `aster-code-review` against the PR #3657 base. Review
maintainability, development correctness/concurrency, security boundaries, and
RISC-V hardware semantics. Fix critical or major findings with a new RED/GREEN
cycle and rerun affected checks.

- [ ] **Step 6: Present the user review package**

Provide:

- exact changed files and line count;
- architecture and data-flow summary;
- RED/GREEN evidence;
- focused and full matrix results;
- persona-review findings;
- `git diff --check`, `git status`, and the complete uncommitted diff.

Stop without staging or committing.
