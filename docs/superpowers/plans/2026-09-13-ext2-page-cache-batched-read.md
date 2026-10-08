# Ext2 Page-cache Batched Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a cold sequential ext2 page-cache read from one synchronous 4-KiB MMC command per page into bounded contiguous multi-page BIOs, without weakening page initialization or error semantics.

**Architecture:** `BackedVmo` locks newly allocated cache pages before publishing them under the XArray lock, then hands those pages and retryable unlocked `Uninit` pages to a new optional backend batch method. Block backends receive page BIO descriptors; ext2 groups adjacent logical pages whose physical blocks are adjacent, reads each run into one bounded bounce segment, and scatters the completed bytes into the locked cache pages. Busy existing pages and non-ext2 backends retain the current single-page path.

**Tech Stack:** Rust 2024, Asterinas page cache and ext2, Aster block BIOs, kernel-mode tests, TLA+/TLC, persistent project Docker, QEMU RISC-V, Milk-V Megrez.

---

### Task 1: Specify batch submission and page-state safety

**Files:**
- Modify: `kernel/src/vm/page_cache/tests/utils.rs`
- Modify: `kernel/src/vm/page_cache/tests/mod.rs`
- Create: `tools/verification/page_cache_batch_publish/PageCacheBatchPublish.tla`
- Create: `tools/verification/page_cache_batch_publish/LockBeforePublish.cfg`
- Create: `tools/verification/page_cache_batch_publish/PublishBeforeLock.cfg`
- Create: `tools/verification/page_cache_batch_publish/run.sh`

- [x] **Step 1: Write a failing page-cache test**

Extend `MockPageCacheBackend` with a batch-call counter and assert that a four-page cold `Vmo::read` invokes one batch backend call while still submitting/completing all four page reads.

- [x] **Step 2: Verify RED**

Run the focused page-cache kernel test in the persistent container. Expected: compilation fails because `PageCacheBackend::read_pages_async` and the batch counter do not exist.

- [x] **Step 3: Model concurrent publication and initialization**

Model two readers acquiring two pages in opposite orders. Check that publishing
before locking admits a wait cycle before either batch is submitted, while
locking missing pages before publishing excludes that cycle. Runtime KTests
cover completion state, submission failure, and retry semantics.

- [x] **Step 4: Run TLC**

Run `tools/verification/page_cache_batch_publish/run.sh`. The negative control
must violate `NoUnsubmittedWaitCycle`; the corrected model must exhaust its
finite state space without errors.

### Task 2: Add the generic bounded multi-page backend contract

**Files:**
- Modify: `kernel/src/vm/page_cache/mod.rs`
- Modify: `kernel/src/vm/page_cache/cache_page.rs`
- Modify: `kernel/src/vm/page_cache/vmo/mod.rs`

- [x] **Step 1: Add the minimal backend API**

Add a default `PageCacheBackend::read_pages_async(Vec<(usize, LockedCachePage)>, &mut IoBatch)` that loops over `read_page_async`, preserving all existing implementations.

- [x] **Step 2: Publish and lock new pages safely**

For read commits, acquire each newly allocated page lock before publishing it in the XArray and batch only those owned guards. Submit the batch after releasing the XArray lock. Handle pre-existing pages with the existing `ensure_init` path so no thread holds one unpublished batch lock while waiting for another thread.

- [x] **Step 3: Verify GREEN and regressions**

Run focused page-cache tests. Expected: the new batch test and existing deferred-I/O, concurrent-reader, and persistent-error tests pass.

### Task 3: Coalesce ext2 page reads into contiguous BIOs

**Files:**
- Modify: `kernel/comps/block/src/bio.rs`
- Modify: `kernel/src/vm/page_cache/mod.rs`
- Modify: `kernel/src/fs/fs_impls/ext2/inode/block_manager/mod.rs`
- Modify: `kernel/src/fs/fs_impls/ext2/test_utils.rs`
- Modify: `kernel/src/fs/fs_impls/ext2/inode/file.rs`

- [x] **Step 1: Write failing ext2 tests**

Add tests proving that four adjacent file pages generate one multi-block device read, a physical discontinuity creates two reads, holes are zero-filled without I/O, and an I/O error leaves affected pages uninitialized and retryable.

- [x] **Step 2: Verify RED**

Run the focused ext2 kernel tests. Expected: adjacent pages still generate one read per page or the batch hook is missing.

- [x] **Step 3: Implement run coalescing**

Add a block-backend batch descriptor and default per-page fallback. In `InodeBlockManager`, split requests at logical gaps, physical gaps, holes, and the 32-page VMO boundary. Read each mapped run into one combined `BioSegment`; its preallocated completion closure copies page-sized slices into the original page segments, then invokes every per-page completion without allocating or blocking in completion context.

- [x] **Step 4: Verify GREEN and full focused suite**

Run the focused page-cache and ext2 kernel tests. The three end-to-end ext2
tests, two run-boundary helpers, and page-cache batching/error/concurrency tests
must pass. A separate attempt to run all 230 KTests reached the unrelated
`tty_echo_runs_without_the_line_discipline_lock` and did not terminate; it is
not used as evidence for this change.

### Task 4: Validate performance and Firefox end to end

**Files:**
- Modify only if needed: `tools/riscv/tests/*` and `tools/riscv/*` benchmark assertions
- Evidence only: ignored `target/firefox-current-performance/*`

- [x] **Step 1: Format, lint, and build**

Run `cargo fmt --all -- --check`, the affected-crate checks, and a RISC-V release kernel build through `tools/docker/run_dev_container.sh`.

- [x] **Step 2: Run QEMU regression tests**

Boot the RISC-V ext2/page-cache tests in QEMU and confirm file contents, sparse holes, errors, concurrent readers, and ordinary desktop gates remain correct.

- [x] **Step 3: Run the Megrez I/O benchmark**

Repeat the established cold 128-MiB direct/buffered comparison. Require successful recovery to RockOS and unchanged default boot-config hashes. Compare buffered throughput against the 3.88-MB/s baseline.

- [x] **Step 4: Run Firefox cold-start verification**

Run the uninstrumented Firefox launcher with the same image/rootfs/config used by the 61.14-second baseline. Record menu-to-console, menu-to-window, user/system ticks, RSS, thread breakdown, evidence hashes, and recovery status.

- [x] **Step 5: Commit verified work**

Commit only the implementation, tests, model, and this plan. Do not push or update the dirty primary `main` worktree without explicit authorization.

## Completed evidence

- TLC: the negative model violated the invariant after 35 generated states;
  the corrected model completed after 69 generated states with no error.
- RISC-V QEMU: adjacent ext2 reads, sparse holes, failed-read retry, generic
  batch dispatch, delayed completion, concurrent faults, and persistent backend
  errors passed as exact KTests.
- RISC-V release build: `TARGET_ARCH=riscv64 SMP=4
  FEATURES=riscv_sv39_mode RELEASE=1` completed offline in the persistent
  development container.
- Megrez: 128-MiB buffered read improved from 34.63 seconds to 15.10 seconds;
  direct I/O remained 13.85 versus 13.71 seconds.
- Firefox: the comparable 61.81-second cold-start baseline became 40.33 and
  46.21 seconds in two independent boots. Both runs found a visible window,
  observed an active service with zero restarts, and recovered to RockOS.

Detailed provenance is recorded in
`docs/porting/evidence/2026-09-13-ext2-page-cache-batched-read.md`.
