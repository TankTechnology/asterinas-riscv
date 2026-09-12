# Fault-around window implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Prevent nonzero-offset file faults from populating pages beyond the existing 16-page window, then measure real Firefox startup.

**Architecture:** Keep the current VM mapping, page-cache backend, and batching policy. Convert the mapped VMO's byte offset to a page index before combining it with the mapping-relative end index. Test backend reads through the real fault handler; do not enable the page-cache profiler, which bypasses batching.

**Tech Stack:** Safe Rust kernel, existing OSDK ktests and RISC-V QEMU, persistent Docker, existing Megrez canary menu.

## Task 1: Regression and minimal fix

**File:** `kernel/src/vm/vmar/vm_mapping.rs` (production expression and existing `tests` module).

- [ ] Add a test backend that records read indices and immediately initializes each requested page. Its `read_page_async` body is:

```rust
self.reads.lock().push(idx);
locked_page.fill_zeros(0, PAGE_SIZE)?;
locked_page.set_up_to_date();
Ok(())
```

  Keep `reads` as `SpinLock<Vec<usize>>` inside a test-only `RecordingPageCacheBackend`. Implement the required write callback as an explicit unexpected-I/O error. Keep the backend's `Arc` alive while testing the weak-backed VMO.

- [ ] Add `fault_around_reads_only_the_mapping_window`, following the adjacent `VmMapping::new`/`handle_page_fault` ktests. For each offset in `[0, 8, 120]`, create a fresh 128-page backend VMO, a read-only 16-page mapping at virtual address `16 * PAGE_SIZE`, and a mapped VMO offset of `offset * PAGE_SIZE`. Invoke the real handler at mapping start. Assert:

```rust
let expected_end = (offset + 16).min(128);
assert_eq!(*backend.reads.lock(), (offset..expected_end).collect::<Vec<_>>());
```

  Also verify the fault page is mapped read-only and its initialized byte is zero. This tests zero-offset compatibility, the nonzero-offset defect, and file-tail clipping with at most 512 KiB backing allocation.

- [ ] Run the ktest before changing production code, retaining the failing log. From `kernel/` in the existing container:

```sh
OSDK_TARGET_ARCH=riscv64 OSDK_LOCAL_DEV=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=4 \
  ../osdk/target/debug/cargo-osdk osdk test fault_around_reads_only_the_mapping_window \
  --scheme riscv --features riscv_sv39_mode \
  --initramfs ../target/megrez-menu/candidate-2/stage1-d62ab8325e03.cpio
```

  Reuse the existing QEMU fixture configuration from prior clock ktests if the scheme requires disk paths. The expected defect is extra reads for offset 8 (pages 8..127 instead of 8..23), not a harness failure.

- [ ] Replace the faulty expression with:

```rust
let end_idx = vmo.offset() / PAGE_SIZE + end_offset.div_ceil(PAGE_SIZE);
```

  Document that the mapped offset is already page aligned. Do not alter page count, I/O policy, error fallback, or unrelated formatting.

- [ ] Rerun the failing test, then all `vm::vmar::vm_mapping::tests` and relevant page-cache tests. Check formatting and whitespace; commit only this scoped file after ordinary review.

## Task 2: Existing canary validation

- [ ] Build an offline release candidate with the same features, preserving Image and bundle identity. Reuse existing Stage1, DTB, Debian and container.
- [ ] Run QEMU Basic/Probe and the existing user-space clock probe on the frozen candidate.
- [ ] Preflight RockOS SSH, unchanged default menu, unmounted Debian filesystem check, and exclusive serial ownership. Stage a separate candidate using `megrez_menu_board`, not by replacing an installed image.
- [ ] Repeat Basic/Probe and full Desktop. Use the retained `desktop_observe.py` and `content_check.py` for the same window, page-JavaScript, DOM-handler and framebuffer criteria. Record cache policy and image hashes.
- [ ] Request software shutdown once; observe firmware and recover RockOS. Record timeout failures honestly, even if later recovery succeeds. Do not issue a second forced reboot or reset.
- [ ] Record before/after observations and remaining limitations in the existing Firefox evidence document. Do not promote over the deployed networking kernel without source parity and the established promotion gates.

## Separate syscall work

Inventory actual unimplemented numbers in retained physical logs against the current architecture table. Consult Linux primary references and existing kernel infrastructure before choosing a small implementation. Keep syscall tests and commits separate from this fault-around change so its performance effect remains identifiable; broad NUMA, pidfd or scheduler interfaces are not presumed trivial.
