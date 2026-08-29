# Megrez SDHCI SDMA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Megrez SD-card controller's installation-blocking word-at-a-time PIO data path with a bounded, firmware-aligned SDMA path while retaining a tested PIO fallback and fail-closed recovery.

**Architecture:** Follow the Megrez RockOS U-Boot choice of SDHCI SDMA, not an unimplemented board-specific SMMU setup. Validate the Linux DT's exact `dma-ranges`, SMMUv3 provider, and SD0 stream ID, but preserve U-Boot's identity-DMA handoff while Asterinas reports no RISC-V IOMMU. Allocate one 512 KiB uncached bounce buffer inside the validated CPU window and give the controller that buffer's physical address. The real SDHCI adapter owns DMA setup, boundary continuation, interrupt decoding, copy-in/out, and data-line recovery. Keep protocol decisions dependency-free and unit-testable; keep every `unsafe` memory operation inside OSTD.

**Tech Stack:** Rust 2024, OSTD safe DMA/frame APIs, Asterinas MMC component ktests, Python `unittest`, pinned project Docker toolchain, Megrez DTB, RockOS U-Boot SDHCI SDMA contract, Linux SDHCI register definitions.

## Frozen reference contract

- RockOS U-Boot commit `444734713e1dc65a525093999830f5bc12fd2b7c`
  enables `CONFIG_MMC_SDHCI_SDMA` in
  [`eic7700_milkv_megrez_defconfig`](https://github.com/rockos-riscv/rockos-u-boot/blob/444734713e1dc65a525093999830f5bc12fd2b7c/configs/eic7700_milkv_megrez_defconfig).
- The board driver calls `sdhci_do_enable_v4_mode` in
  [`eswin_sd_sdhci.c`](https://github.com/rockos-riscv/rockos-u-boot/blob/444734713e1dc65a525093999830f5bc12fd2b7c/drivers/mmc/eswin_sd_sdhci.c).
  Therefore offset `0x00` is the v4 32-bit block-count register, not the
  legacy SDMA address register.
- RockOS writes the 64-bit SDMA address at `0x58/0x5c`, selects a 512 KiB
  boundary, and resumes a boundary interrupt at the next aligned address in
  [`drivers/mmc/sdhci.c`](https://github.com/rockos-riscv/rockos-u-boot/blob/444734713e1dc65a525093999830f5bc12fd2b7c/drivers/mmc/sdhci.c).
- Linux documents the same v4 address selection and 32-bit block-count alias
  in its generic
  [`sdhci.c`](https://github.com/torvalds/linux/blob/master/drivers/mmc/host/sdhci.c)
  and
  [`sdhci.h`](https://github.com/torvalds/linux/blob/master/drivers/mmc/host/sdhci.h).
- RockOS U-Boot's `sd@50460000` node has neither `dma-ranges` nor `iommus`, so
  U-Boot's generic DMA conversion is an identity mapping. RockOS Linux adds
  both `dma-ranges = <0 0x20000000 0 0xc0000000 0 0x40000000>` and
  `iommus = <&smmu0 16>` to the same controller, then attaches it to the ARM
  SMMUv3 driver. The Linux IOVA offset is therefore not a standalone bus
  translation and must not be applied without programming that SMMU stream.
- Asterinas RISC-V currently returns `NoIommu` and
  `has_dma_remapping() == false`. Until that changes, the supported Megrez
  contract is U-Boot identity DMA with owned, uncached memory below 4 GiB.

Any future Megrez MMC change must preserve these address and register facts in
focused tests.

## Frozen pre-board evidence (2026-08-29)

- Physical paging mode is **Sv48**. The generic QEMU fast profile remains Sv39
  and now rejects an Sv48 plan before launching a process; physical plans must
  pass `--paging-mode sv48` rather than inheriting an implicit Sv39 value.
- Kernel Image: `a4cf3a4f40d00dd83d0c057c94ebc5a611a74f32f41ea9198632a627d034d928`
  (`14,698,872` bytes), built from kernel commit `4960dc2d0`.
- Megrez DTB: `02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17`.
  `/soc/mmc@0x50460000` is enabled and records the exact DMA window above.
- U-Boot read-only baseline: `mmc dev 1`, then
  `mmc read 0x90000000 0 0x10000`, read all `65,536` sectors successfully;
  `crc32 0x90000000 0x2000000` returned `5f85f90e`.
- Static read-only probe initramfs:
  `08969066b1f6d2e83ddd98d547783cae34cc4d926df9bb6757d8706818564df5`
  (`479,744` bytes), raw newc entries exactly `.` and `init`. It opens only
  `/dev/mmcblk0` with `O_RDONLY`, reads exactly 32 MiB with positional I/O,
  compares the U-Boot CRC32, emits monotonic UART markers, and reboots.
- QEMU contract approximation reaches the exact static `/init` in about six
  wall-clock seconds and reports the expected `target-open` failure because
  QEMU virt has no EIC7700 SDHCI. QEMU therefore validates Sv48, SMP=4,
  initramfs unpacking, marker output, and recovery only; it does not claim to
  validate the physical controller.
- The first post-fix physical run proved identity DMA on hardware:
  `cpu=0xfff00000 device=0xfff00000`, followed by controller, card, read-only
  block registration, and the 32 MiB read-start marker. The prior
  `0x02008000` SDHCI bus error did not recur. Its host runner then stopped on
  the early design's `partition-table sha256` marker, which had never existed
  in the kernel. The final gate therefore binds the already-defined 32 MiB
  read to U-Boot CRC32 `5f85f90e` instead of accepting imaginary evidence.
- The first physical SDMA run at commit `8127014e997` reached the card through
  PIO, then failed the first 32 MiB transfer with status `0x02008000` after
  programming `0x5ff00000`. Bit 25 is the SDHCI ADMA/system-bus error. The
  allocated CPU buffer was `0xfff00000`; subtracting the Linux IOVA offset
  without an SMMU mapping made the controller access the wrong address. This
  failure is the RED evidence for the identity-DMA correction.
- A later attempt with the same immutable artifacts reached `Enter riscv_boot`
  and then stalled while initializing frame metadata, before component init
  could arm `asterinas.reboot_after`. This exposed a recovery-timing gap rather
  than an SDHCI regression: the normal Asterinas recovery timer cannot recover
  code that hangs before it is initialized. The board workflow now has an
  opt-in pre-`booti` EIC7700X DesignWare watchdog. It validates component type
  and control-register readback, uses interrupt-then-reset mode, and recognizes
  a returned U-Boot banner as a failed current attempt. Host tests cover the
  exact command order and fail-closed behavior.
- The first watchdog-enabled attempt remained fail-closed at U-Boot: the
  watchdog component register read as zero and no kernel was started. Read-only
  follow-up showed `lsp_clk_en0=0xfe3fff83` (WDT0 clock enabled) but
  `wdt_rst_ctrl=0` (all four watchdog resets asserted). This matches the TRM's
  system-controller fields at offsets `0x200` and `0x444` and explains the zero
  component ID. The workflow now preserves both registers' unrelated bits,
  deasserts only WDT0 reset, verifies the prerequisites, and uses EIC7700X's
  real `TOP=0xf` maximum; the upper TORR nibble is reserved on this SoC.
- The final bounded run at `cec8ca25b` proved both fixes on hardware. U-Boot
  read back `lsp_clk_en0=0xfe3fff83`, changed only `wdt_rst_ctrl` bit 0 from 0
  to 1, exposed component type `0x44570120`, and read back
  `CR=0x1f/TORR=0x0f` before `booti`. Asterinas then registered the identity
  SDMA buffer and read-only SDHC device, read exactly 32 MiB in `5.195899`
  seconds, and produced CRC32 `5f85f90e`. The hardware watchdog subsequently
  restarted the board through OpenSBI and the runner stopped U-Boot autoboot;
  both the board result and the independent SDHCI evidence gate report pass.
  The retained serial log SHA-256 is
  `5d4b462ea6c8b934e6f10a7333d675369e0e77c11596f31a04b524c85f4431c3`.

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

Add a conservative `GlobalFrameAllocator::alloc_in` fallback, an explicit
`FrameAllocOptions::alloc_segment_in`, and the OSDK buddy implementation.
Search intrusive free lists without heap allocation, split only the selected
chunk, and return every unused sibling/tail to the correct pool. Constrained
allocations first reclaim the current CPU's small-object cache into the buddy
pool so cached adjacent frames can coalesce before the bounded search. No
out-of-range cached frame can escape the contract.

- [ ] **Step 3: Verify allocator invariants**

Run the focused frame-allocator ktests plus existing allocator tests and a
RISC-V OSDK compile. No MMC code is changed in this task.

### Task 3: Freeze the SDMA and DT contract in dependency-free tests

**Files:**
- Modify: `kernel/comps/mmc/src/sdhci.rs`
- Modify: `kernel/comps/mmc/src/card.rs`
- Modify: `kernel/comps/mmc/src/arch/riscv.rs`

- [ ] **Step 1: Write failing protocol/DT tests**

Require exact Megrez `dma-ranges` (`IOVA 0x20000000`, CPU
`0xc0000000`, size `0x40000000`), SMMUv3 provider and SD0 stream ID 16,
`dma-noncoherent`, SDMA capability, a 512 KiB
maximum request, 512-byte block alignment, checked bus-address translation,
the U-Boot-preserved SDHCI v4 mode, address registers `0x58/0x5c`, the
block-count alias at `0x00`, DMA select/transfer-mode values, 512 KiB boundary
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
uncached CPU view, validate the Linux IOVA/SMMU description, then preserve the
U-Boot identity mapping and retain it in `MmioHost`. For writes, copy into the bounce buffer
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
DMA error, unavailable buffer, and repeated mixed traffic. Require CPU and
device addresses to be identical, aligned, and inside the validated physical
window, and no write to partition two without the existing
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
