# Megrez SDHCI SDMA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Megrez SD-card controller's installation-blocking word-at-a-time PIO data path with a bounded, firmware-aligned SDMA path while retaining a tested PIO fallback and fail-closed recovery.

**Architecture:** Follow the Megrez RockOS U-Boot choice of SDHCI SDMA, not an unproven board-specific ADMA/IOMMU setup. Parse the exact DT `dma-ranges`, allocate one 512 KiB uncached bounce buffer inside the CPU-visible DMA window, translate it to the controller bus address, and let the real SDHCI adapter own DMA setup, boundary continuation, interrupt decoding, copy-in/out, and data-line recovery. Keep protocol decisions dependency-free and unit-testable; keep every `unsafe` memory operation inside OSTD.

**Tech Stack:** Rust 2024, OSTD safe DMA/frame APIs, Asterinas MMC component ktests, Python `unittest`, pinned project Docker toolchain, Megrez DTB, RockOS U-Boot SDHCI SDMA contract, Linux SDHCI register definitions.

---

### Task 1: Preserve high-information installer progress on UART

**Files:**
- Modify: `tools/riscv/debian/rootfs/megrez_installer.py`
- Modify: `tools/riscv/tests/test_megrez_debian_installer.py`

- [ ] **Step 1: Write failing tests**

Require every `DEBIAN_INSTALL_*` progress/failure/pass marker to go through one
`emit` helper that writes a complete line to `/dev/ttyS0`. Require the helper
to be initialized after `devtmpfs` and before the first gate failure. Preserve
the existing hold/reboot behavior.

- [ ] **Step 2: Record RED and implement the narrow fix**

Run the focused Python test, record the direct-`echo` failure, then replace
only marker output with `emit`. Do not change the signed image, chunk protocol,
network URL, or write authorization.

- [ ] **Step 3: Run focused GREEN and static checks**

Run the installer test module, `py_compile`, Ruff check/format, and
`git diff --check`.

### Task 2: Add range-constrained physical frame allocation

**Files:**
- Modify: `ostd/src/mm/frame/allocator.rs`
- Modify: `osdk/deps/frame-allocator/src/lib.rs`
- Modify: `osdk/deps/frame-allocator/src/cache.rs`
- Modify: `osdk/deps/frame-allocator/src/pools/mod.rs`
- Modify: `osdk/deps/frame-allocator/src/set.rs`
- Modify: `ostd/src/mm/frame/linked_list.rs`
- Test: `osdk/deps/frame-allocator/src/test.rs`

- [ ] **Step 1: Write allocator RED tests**

Test an aligned allocation wholly inside a requested physical range, selecting
the correct child of a larger buddy, rejecting empty/unaligned/too-small
ranges, leaving out-of-range chunks available, and restoring total free size
after deallocation.

- [ ] **Step 2: Implement the minimum safe API**

Add a default-failing `GlobalFrameAllocator::alloc_in`, an explicit
`FrameAllocOptions::alloc_segment_in`, and the OSDK buddy implementation.
Search intrusive free lists without heap allocation, split only the selected
chunk, and return every unused sibling/tail to the correct pool. Constrained
allocations bypass the small per-CPU size cache so no out-of-range cached frame
can escape the contract.

- [ ] **Step 3: Verify allocator invariants**

Run the focused frame-allocator ktests plus existing allocator tests and a
RISC-V OSDK compile. No MMC code is changed in this task.

### Task 3: Freeze the SDMA and DT contract in dependency-free tests

**Files:**
- Modify: `kernel/comps/mmc/src/sdhci.rs`
- Modify: `kernel/comps/mmc/src/card.rs`
- Modify: `kernel/comps/mmc/src/arch/riscv.rs`

- [ ] **Step 1: Write failing protocol/DT tests**

Require exact Megrez `dma-ranges` (`device 0x20000000`, CPU
`0xc0000000`, size `0x40000000`), `dma-noncoherent`, SDMA capability, a
512 KiB maximum request, 512-byte block alignment, checked bus-address
translation, SDHCI DMA select/address/transfer-mode values, 512 KiB boundary
continuation, and DMA-error decoding.

- [ ] **Step 2: Implement pure constructors and classifiers**

Add no-MMIO helpers for SDMA address windows, transfer chunks, register values,
interrupt classification, and PIO-fallback decisions. Reject rather than
guess on malformed DT data, unsupported capability, overflow, or address
truncation.

- [ ] **Step 3: Run focused GREEN**

Run only MMC ktests/compile until all pure protocol cases are green.

### Task 4: Add the bounded uncached SDMA bounce buffer

**Files:**
- Modify: `ostd/src/mm/dma/dma_coherent.rs`
- Modify: `kernel/comps/mmc/src/card.rs`
- Modify: `kernel/comps/mmc/src/arch/riscv.rs`
- Modify: `kernel/comps/mmc/src/block.rs`

- [ ] **Step 1: Write failing end-to-end host-model tests**

Require multi-block reads and writes to select SDMA, copy exact bytes through
the bounce buffer, continue at controller boundary interrupts, clear status,
wait for transfer completion, and reset the data line after timeout/DMA/data
errors. Require PIO fallback when the controller lacks SDMA or the safe buffer
cannot be created.

- [ ] **Step 2: Implement safe allocation and transfer ownership**

Expose `DmaCoherent::alloc_in`, convert the non-coherent allocation to an
uncached CPU view, translate its held physical range through the exact DT DMA
window, and retain it in `MmioHost`. For writes, copy into the bounce buffer
before command issue; for reads, copy out only after successful completion.
Program one request at a time and never expose DMA memory directly to the block
BIO layer.

- [ ] **Step 3: Preserve recovery and observability**

On every error, log command/status/address-class information without payload
data, stop/reset the data path, clear pending interrupts, and return a stable
`HostError`. Log one boot-time mode line (`sdma` or `bounded-pio-fallback`) and
bounded transfer-progress counters rather than per-sector noise.

### Task 5: Build the simulation-first pre-board gate

**Files:**
- Modify: `tools/riscv/megrez_sdhci_gate.py`
- Modify: `tools/riscv/tests/test_megrez_sdhci_gate.py`
- Modify: `docs/superpowers/plans/2026-08-24-megrez-sdhci-m2a.md`

- [ ] **Step 1: Add deterministic host gates**

Model reads/writes at 512 B, 4 KiB, 512 KiB, boundary crossing, timeout,
DMA error, unavailable buffer, and repeated mixed traffic. Require no transfer
outside the DT window and no write to partition two without the existing
authorization gate.

- [ ] **Step 2: Run local heavy gates once**

Inside the pinned Docker image run RISC-V MMC/OSTD/frame-allocator ktest
compile, USB/PCI Clippy regression, and `make kernel TARGET_ARCH=riscv64
SMP=4`. Reuse generated initramfs and caches; do not wait for remote CI.

- [ ] **Step 3: Produce a pre-board evidence manifest**

Record commit, DTB/kernel/initramfs hashes, exact DMA window, SDMA model results,
and automatic-recovery command. Refuse a physical run if any identity drifts.

### Task 6: Execute one bounded high-information board run

**Files:**
- Modify only if the run exposes a reproduced defect with a failing test first.

- [ ] **Step 1: Run read-only performance validation**

Boot Asterinas once with automatic recovery. Require the `sdma` mode marker,
read a bounded 32 MiB range, and compare elapsed time plus exact hash against a
known artifact. Abort before writes on timeout, fallback, DMA error, hash
mismatch, or missing UART evidence.

- [ ] **Step 2: Run the resumable Debian installer**

Only after read-only GREEN, arm the existing partition-two/hash gates and run
the chunked installer. Require ordered UART chunk markers, zero network/DMA
fatal counters, final signed root hash, and automatic reboot into the Asterinas
stage1 handoff.

- [ ] **Step 3: Verify the resulting Debian root under Asterinas**

Require stage1 discovery, ext2 handoff, `/bin/bash`, package identity, and the
existing browser/network readiness probes. Keep Linux out of the runtime path;
it may be used only as a host-side artifact source or reference implementation.

- [ ] **Step 4: Commit and push only verified milestones**

Keep serial-observability, allocator, SDMA protocol, SDMA adapter, and board
evidence as separate logical commits. Report any remaining hardware-only
uncertainty explicitly instead of requesting repeated manual resets.
