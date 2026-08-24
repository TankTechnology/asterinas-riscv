---
date: 2026-07-28
mode: files
files: ostd/src/mm/kspace/mod.rs,ostd/src/mm/frame/meta.rs
head: 6e57d226d-dirty
branch: codex/riscv-svade-c1
---

# Summary

The unified mapping-policy refactor has no new blocking defect. Both ordinary
`MappedItem` construction and early frame-metadata mapping now use one
`prepare_new_kernel_mapping_prop` entry point, while raw PTE restoration still
bypasses the policy and preserves status flags exactly.

The two major observations are existing project boundaries rather than
regressions from this change: a persistent forced-Svade CI lane remains planned
for C3, and the kernel's pre-existing writable/executable aliasing policy needs
a separate W^X hardening effort. Most maintainability findings likewise concern
code that predates C1. The two change-local qualified-call findings were fixed
during review; the inline `mapped_item` module remains a deliberate tradeoff to
avoid adding another source file.

## Maintainability

### `ostd/src/mm/frame/meta.rs` line 23

> ```diff
> use crate::mm::{PAGE_SIZE, Paddr, PagingConstsTrait, Vaddr, kspace::FRAME_METADATA_RANGE};
> ```

`qualified-fn-imports` (nit): The `mapping` module imports `PAGE_SIZE` and `FRAME_METADATA_RANGE` directly, so their uses lose the module provenance required for constants.

**Fix.** Import `crate::mm` and `crate::mm::kspace`, then use `mm::PAGE_SIZE` and `kspace::FRAME_METADATA_RANGE`.

### `ostd/src/mm/frame/meta.rs` line 26

> ```diff
> pub(crate) const fn frame_to_meta<C: PagingConstsTrait>(paddr: Paddr) -> Vaddr {
> ...
> pub(crate) const fn meta_to_frame<C: PagingConstsTrait>(vaddr: Vaddr) -> Paddr {
> ```

`least-surprise` (minor): `frame_to_meta<C>` and `meta_to_frame<C>` require callers to provide `C: PagingConstsTrait`, but neither function uses `C`; both use the global `FRAME_METADATA_RANGE`. The irrelevant parameter falsely suggests architecture-dependent behavior and forces unnecessary turbofish syntax.

**Fix.** Remove `C` and the `PagingConstsTrait` bound from both functions, then remove the `<PagingConsts>` arguments from their call sites.

### `ostd/src/mm/frame/meta.rs` line 57

> ```diff
> kspace, paddr_to_vaddr, page_size,
> ...
> panic::abort,
> util::ops::range_difference,
> ```

`qualified-fn-imports` (nit): `paddr_to_vaddr`, `page_size`, `abort`, and `range_difference` are imported as bare free functions, obscuring their module origins at each call site.

**Fix.** Import the `mm`, `panic`, and `util::ops` modules and call `mm::paddr_to_vaddr`, `mm::page_size`, `panic::abort`, and `ops::range_difference`. The new mapping-policy call is already qualified through `kspace`.

### `ostd/src/mm/frame/meta.rs` line 201

> ```diff
> OutOfBound,
> ```

`accurate-names` (nit): `OutOfBound` uses a nonstandard singular name for an address outside a valid range; the conventional and grammatically accurate term is `OutOfBounds`.

**Fix.** Rename the variant to `OutOfBounds` and update its construction and matching sites.

### `ostd/src/mm/frame/meta.rs` line 234

> ```diff
> pub(super) fn get_from_unused<M: AnyFrameMeta>(
>     paddr: Paddr,
>     metadata: M,
>     as_unique_ptr: bool,
> ) -> Result<*const Self, GetFrameError> {
> ```

`no-bool-args` (minor): `as_unique_ptr` selects two distinct initialization modes, and callers pass opaque literals such as `true` and `false`. The name is also misleading because the returned pointer type does not change; the flag changes the reference-count ownership state.

**Fix.** Split this into shared and unique initialization functions, or replace `as_unique_ptr` with a typed enum such as `InitialOwnership::{Shared, Unique}`.

### `ostd/src/mm/frame/meta.rs` line 351

> ```diff
> /// be unsafe. Specifically, the derefernecer should ensure that:
> ```

Typo (nit): The safety documentation contains the misspelling `derefernecer`, which makes an already subtle pointer contract harder to read.

**Fix.** Replace `derefernecer` with `dereferencer`.

### `ostd/src/mm/frame/meta.rs` line 501

> ```diff
> let (range_1, range_2) = allocator::EARLY_ALLOCATOR
>     .lock()
>     .as_ref()
>     .unwrap()
>     .allocated_regions();
> for r in range_difference(&range_1, &meta_page_range) {
> ```

`descriptive-names` (minor): `range_1`, `range_2`, and `r` conceal that the tuple contains the low-memory and high-memory regions allocated during early boot. Readers must inspect `EarlyFrameAllocator::allocated_regions` to recover that meaning, and the ordinal tuple also causes two duplicated loops.

**Fix.** Return an iterable or named representation from `allocated_regions`, then process it using a name such as `allocated_range`; alternatively name the two values explicitly as low- and high-memory ranges.

### `ostd/src/mm/frame/meta.rs` line 575

> ```diff
> macro_rules! mark_ranges {
>     ($region: expr, $typ: expr) => {{
>         debug_assert!($region.base().is_multiple_of(PAGE_SIZE));
>         debug_assert!($region.len().is_multiple_of(PAGE_SIZE));
>
>         let seg = Segment::from_unused($region.base()..$region.end(), |_| $typ).unwrap();
>         let _ = ManuallyDrop::new(seg);
>     }};
> }
> ```

`macros-as-last-resort` (minor): `mark_ranges!` performs ordinary typed control flow and only parameterizes a metadata-producing expression; a generic function can express this without macro expansion. Its plural name is also misleading because each invocation handles one region.

**Fix.** Replace the macro with a generic `mark_range` function that accepts the region and an `FnMut` metadata factory.

### `ostd/src/mm/frame/meta.rs` line 598

> ```diff
> MemoryRegionType::Usable => {} // By default it is initialized as usable.
> ```

`comment-punctuation` (nit): The full-sentence comment after `MemoryRegionType::Usable` lacks terminal punctuation.

**Fix.** End the comment with a period.

### `ostd/src/mm/frame/meta.rs` line 612

> ```diff
> use crate::mm::kspace::LINEAR_MAPPING_BASE_VADDR;
> ```

`qualified-fn-imports` (nit): `LINEAR_MAPPING_BASE_VADDR` is imported directly inside `add_temp_linear_mapping`, hiding that the constant belongs to `kspace`.

**Fix.** Import `crate::mm::kspace` and access the constant as `kspace::LINEAR_MAPPING_BASE_VADDR`.

### `ostd/src/mm/frame/meta.rs` line 614

> ```diff
> const PADDR4G: Paddr = 0x1_0000_0000;
> ```

`dry` (minor): The local `PADDR4G` repeats the same `0x1_0000_0000` early-boot mapping boundary declared in `ostd/src/mm/frame/allocator.rs`. These two sites jointly define which early allocations are addressable, so independent copies can drift.

**Fix.** Define the boundary once under a semantic name such as `BOOT_LINEAR_MAPPING_END_PADDR` and use that shared constant in both the allocator and temporary-mapping code.

### `ostd/src/mm/kspace/mod.rs` line 63

> ```diff
> mm::{PAGE_SIZE, PagingLevel, frame::FrameRef, page_table::largest_pages},
> task::disable_preempt,
> ```

`qualified-fn-imports` (nit): `largest_pages` and `disable_preempt` are imported directly as free functions, so their call sites do not show that they belong to `page_table` and `task`.

**Fix.** Import the `page_table` and `task` modules and call `page_table::largest_pages` and `task::disable_preempt`.

### `ostd/src/mm/kspace/mod.rs` line 70

> ```diff
> const_assert!(PagingConsts::ADDRESS_WIDTH >= 39);
> const ADDR_WIDTH_SHIFT: usize = PagingConsts::ADDRESS_WIDTH - 39;
> ```

`no-magic-number` (minor): The hardware-derived minimum address width `39` is repeated in the assertion and shift calculation. The surrounding comment explains it, but the code still lacks a single named representation of the invariant.

**Fix.** Introduce a constant such as `MIN_ADDRESS_WIDTH_BITS` and derive both `const_assert!` and `ADDR_WIDTH_SHIFT` from it.

### `ostd/src/mm/kspace/mod.rs` line 90

> ```diff
> /// The maximum virtual address of user space (non inclusive).
> ...
> pub const MAX_USERSPACE_VADDR: Vaddr = (0x0000_0040_0000_0000 << ADDR_WIDTH_SHIFT) - PAGE_SIZE;
> ```

`accurate-names` (minor): `MAX_USERSPACE_VADDR` is documented and consumed as an exclusive range endpoint, while `MAX` conventionally denotes the greatest valid value. The name invites off-by-one interpretations despite the parenthetical documentation.

**Fix.** Rename the constant to an endpoint name such as `USERSPACE_END_VADDR` and update its consumers.

### `ostd/src/mm/kspace/mod.rs` line 98

> ```diff
> /// The kernel code is linear mapped to this address.
> ...
> pub fn kernel_loaded_offset() -> usize {
> ```

`rfc1574-summary` (nit): The summary for `kernel_loaded_offset` is a noun-led statement and calls the returned value an address, although the function exposes an offset.

**Fix.** Use a verb-led and behavior-accurate summary such as `Returns the virtual-address offset at which the kernel code is loaded.`

### `ostd/src/mm/kspace/mod.rs` line 131

> ```diff
> /// Convert physical address to virtual address using offset, only available inside `ostd`
> pub fn paddr_to_vaddr(pa: Paddr) -> usize {
> ```

`least-surprise` (minor): `paddr_to_vaddr` promises a virtual address but returns the representation type `usize`, unlike nearby conversion helpers that return `Vaddr`. This discards useful domain meaning from the signature.

**Fix.** Return `Vaddr` and update the summary to `Converts a physical address to its linear-mapping virtual address.`

### `ostd/src/mm/kspace/mod.rs` line 180

> ```diff
> mod mapped_item {
>     //! Kernel page-table item representation and construction policy.
>     ...
>     unsafe impl PageTableConfig for KernelPtConfig {
>         ...
>     }
> }
> ```

`single-responsibility` (minor): The inline `mapped_item` module is a substantial independent abstraction containing its own representation, constructors, raw-restoration policy, and unsafe `PageTableConfig` implementation. Keeping those details inside `kspace/mod.rs` interrupts the high-level kernel-space layout and initialization flow.

**Fix.** Move the module to `ostd/src/mm/kspace/mapped_item.rs` and leave a `mod mapped_item;` declaration plus the necessary re-export in `kspace/mod.rs`.

### `ostd/src/mm/kspace/mod.rs` line 324

> ```diff
> let mut cursor = kpt.cursor_mut(&preempt_guard, &from).unwrap();
> for (pa, level) in largest_pages::<KernelPtConfig>(from.start, 0, max_paddr) {
>     unsafe { cursor.map(MappedItem::untracked(pa, level, prop)) };
> }
> ...
> let mut cursor = kpt.cursor_mut(&preempt_guard, &from).unwrap();
> for (pa, level) in largest_pages::<KernelPtConfig>(from.start, pa_range.start, pa_range.len()) {
>     unsafe { cursor.map(MappedItem::untracked(pa, level, prop)) };
> }
> ```

`dry` (minor): `init_kernel_page_table` repeats the same cursor creation, `largest_pages` iteration, and unsafe `MappedItem::untracked` mapping sequence for the linear map, metadata, and kernel image. The three copies make future mapping-policy changes require synchronized edits.

**Fix.** Extract a private helper that maps an untracked physical range into a virtual range using a supplied `PageProperty`, leaving `init_kernel_page_table` to describe only its three high-level mapping steps.

### `ostd/src/mm/kspace/mod.rs` line 391

> ```diff
> .expect("The kernel page table is not initialized yet");
> ```

`error-message-format` (nit): The `expect` message starts with uppercase `The`, contrary to the lowercase error-message convention.

**Fix.** Change the message to `the kernel page table is not initialized yet`.

## Correctness

### `ostd/src/mm/frame/meta.rs` line 485

> ```diff
>             let prop = kspace::prepare_new_kernel_mapping_prop(PageProperty {
>                 flags: PageFlags::RW,
>                 cache: CachePolicy::Writeback,
>                 priv_flags: PrivilegedPageFlags::GLOBAL,
>             });
>             unsafe { boot_pt.map_base_page(vaddr, frame_paddr, prop) };
> ```

`add-regression-tests` (major): `frame::meta::init()` now depends on `prepare_new_kernel_mapping_prop()` to preset the RISC-V `A` and `D` bits, but no automated test boots with `Svade` forced. If this call regresses, the first metadata write faults during early boot, while ordinary `Svadu` runs silently set the bits and still pass.

**Fix.** Add automated RISC-V `Sv39` and `Sv48` boot-test lanes running `cargo osdk test` with `-cpu rv64,svadu=false,svade=true`. Assert successful metadata initialization and kernel page-table activation, and reference issue `#3589`.

## Security

### `ostd/src/mm/kspace/mod.rs` line 320

> ```diff
> let prop = PageProperty {
>     flags: PageFlags::RW,
>     cache: CachePolicy::Writeback,
>     priv_flags: PrivilegedPageFlags::GLOBAL,
> };
> ...
> let prop = PageProperty {
>     flags: PageFlags::RWX,
>     cache: CachePolicy::Writeback,
>     priv_flags: PrivilegedPageFlags::GLOBAL,
> };
> ```

Writable executable alias (major): The linear map gives every physical page `PageFlags::RW`, including the kernel image, while the same image is mapped with `PageFlags::RWX` below. An attacker who gains a kernel write primitive can therefore modify executable text through either alias, and writable kernel data is executable.

**Fix.** Map linker-defined text, read-only data, and writable data segments with `PageFlags::RX`, `PageFlags::R`, and `PageFlags::RW`, respectively. The linear-map aliases of executable frames must also be read-only or omitted; changing only the image mapping to `PageFlags::RX` would leave a writable alias.

## Retracted by verification

- `ostd/src/mm/kspace/mod.rs` line 189: the change now calls
  `super::prepare_new_kernel_mapping_prop`, so the reported unqualified helper
  call no longer exists.
