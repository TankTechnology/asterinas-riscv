# Megrez DWMAC Document-Driven Preboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the documented Megrez GMAC DMA-domain, DWMAC 5.20 queue, and EIC7700 uncached-alias assumptions into executable preboard gates and one high-information initialization marker.

**Architecture:** Reject unsupported DT address translation in both the offline inspector and exact in-kernel parser. Extend the existing dependency-free Rust host model with DWMAC 5.20 descriptor/tail/status invariants, keep true address ownership in `DmaCoherent`, and expose only a read-only alias diagnostic to the DWMAC initialization path. Use focused host tests for iteration and one RISC-V ktest compile at the end.

**Tech Stack:** Rust 2024/no_std, Asterinas OSTD DMA and ktests, Synopsys DWMAC normal descriptors, EIC7700 non-coherent alias, Python unittest, `rustc --test`, `cargo-osdk`.

---

### Task 1: Reject unsupported GMAC DMA translation

**Files:**
- Modify: `tools/riscv/tests/test_megrez_gmac_contract.py`
- Modify: `tools/riscv/megrez_gmac_contract.py`
- Modify: `kernel/comps/dwmac/src/arch/riscv.rs`

- [ ] **Step 1: Write the offline inspector RED**

Extend `_FakeFdtget` with an `extra_properties` mapping and add one test that
injects `iommus` and `dma-ranges` into each GMAC node. For each property,
`inspect_dtb` must raise `ContractError` naming
`ethernetN.<property>: unsupported DMA translation` before reading later
properties.

- [ ] **Step 2: Run the RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_gmac_contract.MegrezGmacContractTests.test_inspect_dtb_rejects_unsupported_dma_translation -v
```

Expected: FAIL because the current inspector accepts the injected property.

- [ ] **Step 3: Implement the minimum inspector rejection**

Immediately after enumerating each port's properties, reject either forbidden
name. Do not add those properties to the frozen JSON schema; absence is the
accepted contract.

- [ ] **Step 4: Add the in-kernel RED contract**

Add `iommu_absent` and `dma_ranges_absent` to `PortFields`, set both to `true`
in `expected_fields` and the frozen fixtures, and add both false mutations to
`rejects_missing_or_drifted_resources_without_fallbacks`. Before production
parsing is updated, the ktest source must fail to compile because the real
initializer omits the fields.

- [ ] **Step 5: Implement the in-kernel parser**

Set the fields from exact property absence:

```rust
iommu_absent: node.property("iommus").is_none(),
dma_ranges_absent: node.property("dma-ranges").is_none(),
```

- [ ] **Step 6: Run focused GREEN**

```bash
make test_riscv_megrez_gmac_unit
```

Expected: all host contract tests pass. The Rust ktest is compiled by the final
RISC-V gate in Task 5.

- [ ] **Step 7: Commit**

```bash
git add tools/riscv/tests/test_megrez_gmac_contract.py \
  tools/riscv/megrez_gmac_contract.py kernel/comps/dwmac/src/arch/riscv.rs
git commit -m "fix(riscv): reject translated Megrez GMAC DMA"
```

### Task 2: Pin the EIC7700 DWMAC revision

**Files:**
- Modify: `kernel/comps/dwmac/src/arch/riscv.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Write revision RED tests**

Add an in-kernel test with fake MAC_VERSION reads requiring `[0x52, 0x52]` to
pass and `0x51`, `0x53`, and a mismatched pair to return
`UnsupportedController`. Add a bounded host source-contract assertion requiring
`const EIC7700_DWMAC_VERSION: u8 = 0x52` and exact equality in
`validate_controllers`.

- [ ] **Step 2: Run the host RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model.DwmacRxPollContractTests.test_megrez_requires_documented_dwmac_5_20 -v
```

Expected: FAIL because production currently accepts `0x40..=0x5f`.

- [ ] **Step 3: Implement exact revision validation**

Replace the broad range constants with `EIC7700_DWMAC_VERSION` and reject any
read low byte not equal to `0x52`. Keep the observed byte in `SelectedPortInfo`
and existing initialization log.

- [ ] **Step 4: Run focused GREEN**

Run the exact RED command and require it to pass.

- [ ] **Step 5: Commit**

```bash
git add kernel/comps/dwmac/src/arch/riscv.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "fix(riscv): pin EIC7700 DWMAC revision"
```

### Task 3: Extend the DWMAC 5.20 host reference model

**Files:**
- Modify: `tools/riscv/dwmac_tx_cacheline_model.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Write four failing reference-model tests**

Add pure model functions and tests for:

```rust
normal_rx_descriptor(0x1_2345_6789, 2048)
normal_tx_descriptor(0x1_2345_6789, 1514)
ring_contract(base, 64, 16)
acknowledge_channel_status(observed, known_mask)
```

Require words 0/1 to preserve the 64-bit address; RX control to contain OWN,
IOC, BUF1V; TX control to contain OWN, FD, LD, exact length; ring length 63,
initial TX base, initial RX one-past, modulo next tails; and W1C to acknowledge
only known bits while preserving unknown state in the model.

- [ ] **Step 2: Run RED**

Change the Python expected model total from `5 passed` to `9 passed`, then run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model.DwmacRxPollContractTests.test_tx_cacheline_model_exposes_packed_descriptor_race -v
```

Expected: FAIL with only five model tests executed.

- [ ] **Step 3: Implement the minimal pure model**

Use fixed documented bit constants, checked ring arithmetic, and no filesystem,
MMIO, threads, or allocation. Keep the file dependency-free and below 250
lines.

- [ ] **Step 4: Run model GREEN**

Run the exact RED command and then:

```bash
make test_riscv_dwmac_rx_model
```

Expected: the Rust model reports `9 passed`; the state-space liveness model and
all source contracts also pass.

- [ ] **Step 5: Commit**

```bash
git add tools/riscv/dwmac_tx_cacheline_model.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(riscv): model DWMAC 5.20 queue contract"
```

### Task 4: Expose bounded DMA address diagnostics

**Files:**
- Modify: `ostd/src/mm/dma/dma_coherent.rs`
- Modify: `ostd/src/mm/dma/test.rs`
- Modify: `kernel/comps/dwmac/src/queue.rs`
- Modify: `kernel/comps/dwmac/src/device.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Write the diagnostic RED**

Add OSTD ktests requiring `uncached_alias_paddr()` to be `None` before an
alias is attached, to preserve the alias start across `Split`, and to leave
`paddr`, `daddr`, and size unchanged. Extend the host source contract to require
one `ASTERINAS_GMAC_DMA_CONTRACT` marker with `ring_paddr`, `ring_daddr`, and
`ring_cpu_alias` fields. It must fail before production fields/logging exist.

- [ ] **Step 2: Run the host RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model.DwmacRxPollContractTests.test_device_emits_dma_address_contract -v
```

Expected: FAIL because the marker and physical/alias fields are absent.

- [ ] **Step 3: Add the read-only OSTD accessor**

Implement:

```rust
pub fn uncached_alias_paddr(&self) -> Option<Paddr> {
    self.uncached_alias.as_ref().map(HasPaddr::paddr)
}
```

No pointer, writer, or ownership API is exposed.

- [ ] **Step 4: Carry and log the three address domains**

Import `HasPaddr` in `queue.rs`; add `ring_paddr` and
`ring_cpu_alias: Option<usize>` to `QueueAddresses`; populate them from the
owned ring. Emit one initialization marker containing the selected `version`,
the physical and DMA ring bases, optional alias, and TX/RX/tail addresses.

- [ ] **Step 5: Run focused GREEN**

```bash
make test_riscv_dwmac_rx_model
```

Expected: all host/model/source-contract tests pass.

- [ ] **Step 6: Commit**

```bash
git add ostd/src/mm/dma/dma_coherent.rs ostd/src/mm/dma/test.rs \
  kernel/comps/dwmac/src/queue.rs kernel/comps/dwmac/src/device.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(riscv): expose Megrez GMAC DMA domains"
```

### Task 5: Run the bounded preboard gate and update evidence

**Files:**
- Modify: `docs/porting/evidence/riscv-dma-memory-type-contract.md`
- Modify: `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`

- [ ] **Step 1: Run one combined host/static gate**

```bash
make test_riscv_megrez_gmac_unit test_riscv_dwmac_rx_model
python3 -m py_compile tools/riscv/megrez_gmac_contract.py \
  tools/riscv/tests/test_megrez_gmac_contract.py \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
ruff check tools/riscv/megrez_gmac_contract.py \
  tools/riscv/tests/test_megrez_gmac_contract.py \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
ruff format --check tools/riscv/megrez_gmac_contract.py \
  tools/riscv/tests/test_megrez_gmac_contract.py \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 2: Run one pinned RISC-V compile gate**

Inside `asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached`, with the
existing pinned toolchain cache and proxy only if the local cache misses, run:

```bash
cargo osdk check --ktests -p ostd -p aster-dwmac -p aster-network \
  -p aster-kernel --target riscv64imac-unknown-none-elf
```

Expected: exit zero. Inspect active output instead of silently waiting; do not
run QEMU or the board.

- [ ] **Step 3: Record only fresh evidence**

Update the two evidence documents with the exact test commands, result counts,
commit identity, and the next-board marker interpretation. State explicitly
that no physical result is claimed.

- [ ] **Step 4: Re-run cheap documentation checks and commit**

```bash
rg -n "iommus|dma-ranges|0x52|ASTERINAS_GMAC_DMA_CONTRACT" \
  docs/porting/evidence tools/riscv kernel/comps/dwmac ostd/src/mm/dma
git diff --check
git add docs/porting/evidence/riscv-dma-memory-type-contract.md \
  docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md
git commit -m "docs(riscv): seal DWMAC preboard contract"
```

- [ ] **Step 5: Final handoff**

Report the commits and exact gates. The next physical transaction, handled by
the existing high-information board plan, should run once and classify the
logged `ring_paddr`, `ring_daddr`, alias, version, TX reclaim, and RX progress.
