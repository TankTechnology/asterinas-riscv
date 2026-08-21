---
date: 2026-07-28
mode: files
files: ostd/src/arch/riscv/boot/bsp_boot.S:9-16,78-118,ostd/src/mm/frame/meta.rs:469-495,ostd/src/mm/kspace/kvirt_area.rs:90-105,136-149,186-199,ostd/src/mm/kspace/mod.rs:44-68,140-294,316-378,ostd/src/mm/kspace/test.rs:1-165,ostd/src/mm/mod.rs:35-43,ostd/src/mm/page_prop.rs:80-210,ostd/src/mm/page_table/mod.rs:580-635,ostd/src/mm/page_table/node/entry.rs:59-105,ostd/src/mm/page_table/node/mod.rs:43-52,239-280,ostd/src/mm/page_table/test.rs:1-15,606-625,948-1090
head: 6e57d226d-dirty
branch: codex/riscv-svade-c1
title: "RISC-V Svade C1 final code review"
---

# Summary

The reviewed C1 implementation has no unresolved defect that blocks its stated
kernel-mapping scope. It keeps PTE encoding exact, presets A/D only when new
RISC-V kernel mappings are constructed, preserves raw restored properties, and
uses CAS to retain only hardware status bits that appeared concurrently. The
security pass reported no findings in the changed ranges.

Two major findings are confirmed boundaries rather than missing C1
implementation: the repository has no persistent forced-Svade test lane (owned
by the planned C3), and user mappings still need A/D fault repair (the planned
C2/C3 path). Both were exercised or analyzed explicitly during this review and
must remain visible in the follow-up PRs.

The remaining two minor findings are maintainability tradeoffs: early frame
metadata and regular kernel mappings apply the shared typed access operation at
separate construction layers, and the private `mapped_item` abstraction remains
inline to avoid adding another source file. This intentionally favors the
requested compact file layout for C1.

During review, the implementation was tightened further: the CAS retry now
merges only newly observed A/D bits, the test-only kernel-area query rejects its
exclusive end address, visibility was narrowed, safety comments were added, and
Sv39/module documentation and test diagnostics were corrected.

## Maintainability

### `ostd/src/mm/frame/meta.rs` line 487

> ```diff
> +            let flags = PageFlags::RW;
> +            #[cfg(target_arch = "riscv64")]
> +            let flags = {
> +                let mut flags = flags;
> +                flags.record_access(PageAccess::Write);
> +                flags
> +            };
> ```

`coupling-cohesion` (minor): `frame::meta::init` now owns a RISC-V-specific branch that presets access state, while `kspace::mapped_item::prepare_new_mapping_prop` separately owns the same Svade policy for other kernel mappings. The architecture rule is split between generic frame-metadata initialization and the kernel-mapping abstraction, making future policy changes require coordinated edits in unrelated modules.

**Fix.** Extract a crate-internal helper such as `prepare_new_kernel_mapping_prop` into a shared MM or architecture-policy module and call it from both `frame::meta::init` and the `MappedItem` constructors, keeping the `riscv64` conditional inside that helper.

### `ostd/src/mm/kspace/mod.rs` line 153

> ```diff
>  pub(super) use mapped_item::MappedItem;
>  
> +mod mapped_item {
> +    //! Kernel page-table item representation and construction policy.
> ```

`single-responsibility` (minor): The new inline `mapped_item` module is a significant abstraction containing `MappedItem`, `MappedItemKind`, constructors, architecture policy, and the complete `PageTableConfig` implementation, while its associated `MappedItemRef` remains in the parent. This splits one abstraction across boundaries and puts roughly `140` lines of implementation detail before `init_kernel_page_table` in `kspace/mod.rs`.

**Fix.** Move the inline module to `ostd/src/mm/kspace/mapped_item.rs`, move `MappedItemRef` with the other item representation types, and leave only `mod mapped_item` plus narrow re-exports in `kspace/mod.rs`.

## Correctness

### `ostd/src/arch/riscv/boot/bsp_boot.S` line 84

> ```diff
> # Early boot has no recovery handler, so writable leaves preset A|D; non-leaf
> # A/D bits are reserved and remain zero.
> PTE_VRWXAD = PTE_V | PTE_R | PTE_W | PTE_X | PTE_A | PTE_D
> ```

`add-regression-tests` (major): No committed test lane boots these early leaves
with Svade forced. Removing `PTE_A | PTE_D`—or omitting the RISC-V
`record_access(PageAccess::Write)` metadata initialization—can therefore pass
normal Svadu CI because hardware sets the missing status bits. The manual
Sv39/Sv48 forced-Svade matrix passed during this review, but it is not a
persistent regression guard.

**Fix.** Add automated `Sv39` and `Sv48` OSTD kernel-test lanes that run QEMU with `-cpu rv64,svadu=false,svade=true`, assert successful completion, and reference `#3589`; they must exercise both the assembly leaves and frame-metadata initialization.

## Hardware

### `ostd/src/mm/kspace/mod.rs` line 211

> ```diff
> fn prepare_new_mapping_prop(prop: PageProperty) -> PageProperty {
>     #[cfg(target_arch = "riscv64")]
>     {
>         let mut prop = prop;
>         let access = if prop.flags.contains(PageFlags::W) {
>             PageAccess::Write
>         } else {
>             PageAccess::Read
>         };
>         prop.flags.record_access(access);
> ```

Incomplete Svade handling (major): `prepare_new_mapping_prop()` applies the eager `A/D` policy only to `KernelPtConfig`, so user mappings still cannot recover from status-bit faults under `Svade`. A writable anonymous mapping first read by userspace is installed as `PageFlags::RW | PageFlags::ACCESSED` with `PageFlags::DIRTY` clear; its first write faults, but `handle_single_page_fault()` sees that `PageFlags::W` already satisfies the requested permission, merely flushes the TLB, and returns without setting `PageFlags::DIRTY`, causing the instruction to fault repeatedly.

**Fix.** In `kernel/src/vm/vmar/vm_mapping.rs`, make the already-permitted branch of `handle_single_page_fault()` update `PageFlags::ACCESSED` and, for a write fault, `PageFlags::DIRTY` through `cursor.protect_next()` before flushing the TLB. Add a forced-`Svade` regression that reads and then writes a writable anonymous mapping.

## Retracted by verification

- `PageAccess` public visibility: retracted because the approved C3 design has
  a separate kernel-crate A/D fault handler that must consume this typed API
  through the public `ostd::mm` boundary.
- Qualified page-table test helpers: retracted because applying the suggested
  qualification produced `unused-qualifications` warnings under the existing
  `use super::*` structure and project lint configuration; the current bare
  calls are the compiler-preferred form here.
