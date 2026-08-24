# Megrez SDHCI M2a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Asterinas discover the removable SD card on Milk-V Megrez and expose it as a strictly read-only block device whose partition table can be read and verified on real hardware.

**Architecture:** Add a safe `aster-mmc` component with a hardware-independent SD protocol core and a RISC-V EIC7700 SDHCI adapter. The first milestone uses bounded polling and PIO only, accepts one frozen DTB resource contract, registers an `mmcblk0` read-only block device, and never issues a card write command.

**Tech Stack:** Rust `no_std`, Asterinas component initialization, OSTD `IoMem`, FDT parsing, `aster-block`, OSDK kernel tests, QEMU RISC-V compile regression, and a serial-driven Megrez read-only gate.

---

## Scope and invariants

- Target only `/soc/mmc@0x50460000`, compatible `eswin,sdhci-sdio`, MMIO `0x50460000..0x5046ffff`, IRQ 81, 4-bit bus, `no-mmc`, and its exact `eswin,syscrg_csr` core-clock resource (`0x51828000`, offset `0x164`).
- Do not bind the eMMC controller at `0x50450000`.
- Use SDHCI PIO and `CMD17` single-block reads. Multiple-sector BIOs are served by bounded repeated `CMD17` operations.
- No DMA, write commands, discard, format, partition edits, or partition-2 provisioning in M2a.
- All polling loops have explicit iteration/deadline bounds and surface stable errors.
- The kernel/component remains `#![deny(unsafe_code)]`; MMIO goes through OSTD safe APIs.
- QEMU proves generic regressions only. Acceptance requires Megrez serial evidence from Asterinas.

## Task 1: Component scaffold and frozen register contract

**Files:**

- Create: `kernel/comps/mmc/Cargo.toml`
- Create: `kernel/comps/mmc/src/lib.rs`
- Create: `kernel/comps/mmc/src/sdhci.rs`
- Modify: `Cargo.toml`
- Modify: `kernel/Cargo.toml`
- Modify: `kernel/src/lib.rs`

**Step 1: Write failing kernel tests**

Add `#[cfg(ktest)]` tests for:

- standard SDHCI offsets and command/transfer-mode encoding;
- interrupt error decoding;
- EIC7700 clock-stability bit masks and SD delay code `0x55`;
- response type selection for `CMD0`, `CMD8`, `CMD17`, `CMD55`, and ACMDs.

**Step 2: Confirm RED**

Run:

```bash
cargo osdk check --ktests -p aster-mmc \
  --target riscv64imac-unknown-none-elf
```

Expected: failure because `aster-mmc` or its types do not exist.

**Step 3: Implement the minimum register model**

Define typed constants and values only; do not access hardware yet:

```rust
pub enum ResponseType { None, Short, ShortBusy, Long }

pub struct Command {
    pub index: u8,
    pub argument: u32,
    pub response: ResponseType,
    pub data: Option<DataDirection>,
}

pub enum HostError {
    Timeout,
    CommandCrc,
    CommandIndex,
    DataCrc,
    DataEndBit,
    Unsupported,
}
```

Use the standard SDHCI register layout and isolate EIC7700 constants in a small private module. Cite the upstream Linux `sdhci.h` and `sdhci-of-dwcmshc.c` as hardware references without copying their subsystem architecture.

**Step 4: Run GREEN and commit**

```bash
cargo osdk check --ktests -p aster-mmc \
  --target riscv64imac-unknown-none-elf
RUSTFLAGS=-Dwarnings cargo clippy -p aster-mmc \
  --target riscv64imac-unknown-none-elf --no-deps
```

Commit: `feat(riscv): define Megrez SDHCI contract`

## Task 2: Bounded SD command engine and card discovery

**Files:**

- Create: `kernel/comps/mmc/src/card.rs`
- Modify: `kernel/comps/mmc/src/sdhci.rs`

**Step 1: Write a fake-host test model**

Define a private test host that records register operations and supplies scripted responses. Cover exact command order:

```text
reset -> clock 400 kHz -> CMD0 -> CMD8 ->
(CMD55, ACMD41)* -> CMD2 -> CMD3 -> CMD9 -> CMD7 ->
CMD55 -> ACMD6 -> clock 25 MHz
```

Tests must cover:

- SDHC success and byte-addressed SDSC rejection;
- ACMD41 timeout and command error;
- invalid CMD8 echo;
- RCA extraction;
- CSD v2 capacity math with checked arithmetic;
- reset, inhibit, command, and data timeouts;
- DWC MSHC command-complete clearing after command-line reset.

**Step 2: Confirm RED, implement, and run GREEN**

Use a narrow trait:

```rust
trait HostController {
    fn reset(&mut self) -> Result<(), HostError>;
    fn set_clock(&mut self, hz: u32) -> Result<(), HostError>;
    fn command(&mut self, command: Command) -> Result<Response, HostError>;
    fn set_bus_width_4(&mut self) -> Result<(), HostError>;
}
```

`Card::discover` returns an immutable identity containing RCA and sector count. Retry counts are constants and tests assert exhaustion exactly.

Run the focused OSDK check and Clippy from Task 1.

Commit: `feat(riscv): discover SDHC cards with bounded commands`

## Task 3: Read-only PIO sectors

**Files:**

- Modify: `kernel/comps/mmc/src/sdhci.rs`
- Modify: `kernel/comps/mmc/src/card.rs`

**Step 1: Write failing PIO tests**

Cover:

- `CMD17` argument equals the SDHC LBA, never a byte address;
- exactly 128 32-bit data-port reads fill one 512-byte sector;
- buffer-ready and transfer-complete are both required;
- error interrupts stop the transfer and reset the data line;
- LBA at capacity and arithmetic overflow fail before MMIO;
- a multi-sector read stops on the first error and never exceeds capacity;
- any write API is absent; block-layer writes later return `NotSupported`.

**Step 2: Implement minimal PIO**

```rust
pub fn read_sector(&mut self, lba: u64, out: &mut [u8; 512]) -> Result<(), HostError>;
pub fn read_sectors(&mut self, first_lba: u64, out: &mut [u8]) -> Result<(), HostError>;
```

No write method is introduced. Use little-endian conversion explicitly.

**Step 3: Run GREEN and commit**

Run the focused OSDK check and Clippy.

Commit: `feat(riscv): read Megrez SD sectors with bounded PIO`

## Task 4: Strict DTB binding and real MMIO adapter

**Files:**

- Create: `kernel/comps/mmc/src/arch/mod.rs`
- Create: `kernel/comps/mmc/src/arch/riscv.rs`
- Create: `kernel/comps/mmc/src/arch/other.rs`
- Modify: `kernel/comps/mmc/src/lib.rs`

**Step 1: Write pure resource-parser tests**

Extract a pure validator and test rejection of:

- missing or multiple candidates;
- wrong compatible, base, length, IRQ, or bus width;
- missing `no-mmc` or disabled status;
- the eMMC address;
- truncated/multiple `reg` resources.

The accepted value is exact:

```rust
PlatformConfig {
    mmio: 0x5046_0000..0x5047_0000,
    clock_mmio: 0x5182_8000..0x5182_9000,
    irq: 81,
    bus_width: 4,
    max_frequency: 208_000_000,
}
```

**Step 2: Implement the adapter**

- Find exactly one accepted node in `DEVICE_TREE`.
- Acquire the exact range with `IoMem::acquire`.
- Implement 8/16/32-bit register reads/writes with bounds and alignment checks.
- Reset the command/data lines, preserve the U-Boot-initialized EIC7700 PHY,
  and program the vendor CRG core divider for 400 kHz discovery and 25 MHz
  data. The standard SDHCI divider is not the authoritative EIC7700 clock
  control.
- Poll synchronously in M2a; IRQ 81 is validated and recorded but not enabled until a later performance milestone.
- On non-RISC-V, initialization is an explicit no-op.

**Step 3: Run GREEN and commit**

```bash
cargo osdk check --ktests -p aster-mmc -p aster-kernel \
  --target riscv64imac-unknown-none-elf
RUSTFLAGS=-Dwarnings cargo clippy -p aster-mmc \
  --target riscv64imac-unknown-none-elf --no-deps
```

Commit: `feat(riscv): bind Megrez SDHCI from the device tree`

## Task 5: Read-only Asterinas block device

**Files:**

- Create: `kernel/comps/mmc/src/block.rs`
- Modify: `kernel/comps/mmc/src/lib.rs`

**Step 1: Write failing adapter tests**

Test:

- stable whole-device name `mmcblk0` and partition names `mmcblk0p1..`;
- exact capacity propagation;
- read BIO segment filling across sector boundaries;
- `Write` returns `BioStatus::NotSupported` without host operations;
- `Flush` completes because M2a has no volatile write state;
- out-of-range and malformed segment sizes return `IoError`;
- partition unregister/re-register behavior matches other block drivers.

**Step 2: Implement and register**

Initialize card discovery synchronously during component init, construct one `Arc` block device, register it through `aster_block::register`, and call the existing BIO segment pool initialization. Keep a spin lock only around the synchronous host/card state.

The first-process partition parser will reuse the existing registry path. No new partition parser is added.

**Step 3: Run GREEN and commit**

Run focused OSDK check, Clippy, and a kernel check.

Commit: `feat(riscv): register Megrez SD as read-only block storage`

## Task 6: Local regression and real-board read-only gate

**Files:**

- Create: `tools/riscv/megrez_sdhci_gate.py`
- Create: `tools/riscv/tests/test_megrez_sdhci_gate.py`
- Modify: `tools/riscv/README.md`

**Step 1: Test the operator gate without hardware**

The gate parses a bounded serial transcript and requires, in order:

```text
[mmc] controller 0x50460000 irq=81 read-only
[mmc] SDHC rca=... sectors=...
[mmc] mmcblk0 registered read-only
[mmc] partition-table sha256=...
```

It rejects panic/fatal markers, duplicate/out-of-order markers, missing capacity, write-enabled text, oversized logs, and timeouts. Output is an atomic JSON result plus complete serial log.

**Step 2: Run proportional local gates**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_sdhci_gate -v
make test_riscv_debian_rootfs_unit
cargo osdk check --ktests -p ostd -p aster-mmc -p aster-kernel \
  --target riscv64imac-unknown-none-elf
RUSTFLAGS=-Dwarnings cargo clippy -p aster-mmc \
  --target riscv64imac-unknown-none-elf --no-deps
```

Run the existing RISC-V SMP=4 QEMU kernel tests once at the milestone boundary. Do not repeat already-green unrelated host suites.

**Step 3: Build and boot Megrez read-only**

- Build the current Sv48 Megrez kernel with the frozen working DTB/initramfs.
- Boot through U-Boot and record full serial output.
- Require Asterinas to register `mmcblk0` and enumerate the existing partition table.
- Read sector 0 and the partition-entry sectors repeatedly through Asterinas.
- Compare SHA-256 evidence with a U-Boot read of the same sectors.
- Confirm no block write command was issued and p1/p2/p3 metadata is unchanged.

If the board fails, preserve the exact transcript and stop at the first hardware-contract mismatch. Do not fall back to Linux and do not write partition 2.

**Step 4: Commit evidence tooling/docs**

Commit: `test(riscv): gate Megrez SDHCI read-only bring-up`

## Milestone completion criteria

M2a is complete only when all of the following are true:

1. `aster-mmc` has bounded model tests and clean RISC-V compile/Clippy results.
2. Asterinas, not Linux, initializes the removable EIC7700 SD controller.
3. `mmcblk0` is registered read-only and the existing partition table is readable.
4. Real-board Asterinas sector evidence matches U-Boot evidence.
5. No write operation or partition mutation occurred.

M2b begins only after this checkpoint and will separately design bounded writes and partition-2 provisioning.
