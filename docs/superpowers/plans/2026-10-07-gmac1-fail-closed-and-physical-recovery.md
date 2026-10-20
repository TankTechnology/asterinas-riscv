# GMAC-1 Fail-Closed Probe and Physical Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the EIC7700 GMAC-1 probe deterministic and fail-closed, identify and implement its clock/reset/platform prerequisites, and qualify one recoverable physical probe without enabling PHY, DMA, TX/RX, or network traffic.

**Architecture:** Treat GMAC-1 as a gated MMIO device. Device-tree and platform-resource validation happens before any GMAC register access; the probe may read the GMAC block only after a bounded, idempotent platform-readiness contract has been proven. QEMU models both the gated and ready states and is the mandatory gate before one watchdog-protected physical attempt.

**Tech Stack:** Rust `no_std` Asterinas `dwmac`, EIC7700 device-tree and MMIO glue, QEMU C `MemoryRegion`/qtest, Python host-side evidence validators, serial/U-Boot watchdog recovery receipts.

---

## Current failure boundary

The latest physical receipt is `target/megrez-physical-step1/gmac-1-cycle-20261007-r12/`. It reaches:

```text
ASTERINAS_PROBE_RESOURCE device=gmac-1 base=0x50410000 size=0x10000 irq=70
ASTERINAS_PROBE_DT device=gmac-1 clocks=[3, 550, 3, 551, 3, 553] resets=[20, 7, 33554432]
ASTERINAS_PROBE_PHASE device=gmac-1 phase=mmio-map
ASTERINAS_PROBE_PHASE device=gmac-1 phase=mmio-read
ASTERINAS_PROBE_ATTEMPT device=gmac-1 offset=0x0000
```

There is no `ASTERINAS_PROBE_READ`, `PASS`, or `DONE` marker. The watchdog returns the board to RockOS, so the failure is classified as `mmio-read:0x0000`, before PHY, MDIO, DMA, TX/RX, interrupt, or network activity. A normal software timeout cannot interrupt a CPU blocked in an external MMIO load; readiness must therefore be established before this load.

## Files and responsibilities

- Modify `kernel/comps/dwmac/src/arch/riscv.rs`: split DT/resource validation from platform readiness; add the GMAC-1 fail-closed preflight and the minimal, ordered platform sequence once QEMU proves it.
- Modify `kernel/comps/dwmac/src/lib.rs` only if the probe entry needs a new explicit result/marker path; keep normal registration and PHY diagnostic paths unchanged.
- Modify `kernel/comps/cmdline/src/lib.rs` only if a separate boot argument is required to select `preflight`, `readonly`, or later `phy` modes; do not overload the existing normal network path.
- Modify `tools/riscv/megrez_hardware_probe.py`: parse `PLATFORM_CHECK`, `PLATFORM_READY`, `PLATFORM_NOT_READY`, and terminal failure markers while retaining the existing fixed-offset and timeout validation.
- Modify `tools/riscv/megrez_menu_board.py` and `tools/riscv/megrez_gmac_gate.py`: require the new preflight evidence, one watchdog, one attempt, and a fresh recovery epoch before allowing the physical leg.
- Add/extend `tools/riscv/tests/test_megrez_hardware_probe.py` and `tools/riscv/tests/test_megrez_gmac_gate.py`: exercise ordering, offset whitelist, fail-stop behavior, and recovery requirements.
- Modify QEMU `/home/ubuntu/.codex/qemu-eic7700-10.2.1/hw/net/eic7700-gmac.c` and `/home/ubuntu/.codex/qemu-eic7700-10.2.1/hw/misc/eic7700.c`: model gated GMAC-1, platform state transitions, reset isolation, and non-blocking safe reads.
- Extend `/home/ubuntu/.codex/qemu-eic7700-10.2.1/tests/qtest/eic7700-dwc3-test.c` and its `tests/qtest/meson.build` entry with GMAC-1 gate/preflight tests; keep descriptor and external-netdev tests separate from the read-only gate.
- Add evidence under `docs/porting/evidence/` and receipts under `target/eic7700-qemu/` and `target/megrez-physical-step1/`; never store credentials.

## Phase 0: Freeze the recovery boundary

**Purpose:** Prevent another board hang while the cause is being narrowed.

- [ ] Preserve the current `r12` serial log and result JSON. Do not retry GMAC-1 physically, start PHY diagnostics, enable normal GMAC registration, send network traffic, or edit persistent U-Boot entries.
- [ ] Record the current board as RockOS with a fresh boot ID, and record the stable serial path and single-owner rule in the next evidence note.
- [ ] Run only host-side tests while the physical leg is locked: `python3 -m pytest tools/riscv/tests/test_megrez_hardware_probe.py tools/riscv/tests/test_megrez_gmac_gate.py -q`.
- [ ] Define the terminal states used by every later run: `platform-not-ready` (no GMAC MMIO touched), `mmio-read-timeout` (unexpected hardware stop after readiness), `readback-mismatch`, and `pass` (six reads plus recovery).

Expected result: no physical action is permitted until the QEMU and static gates below produce a new receipt.

## Phase 1: Establish the hardware contract from existing sources

**Purpose:** Determine whether the first read is blocked by an unenabled clock/reset/HSP/RGMII domain and avoid inventing register semantics.

- [ ] Extract the physical EIC7700 DTB used by RockOS and save the GMAC-0/GMAC-1 nodes, their `reg`, `interrupts`, `clocks`, `resets`, `rst-gpios`, `eswin,hsp`, RGMII/pinctrl references, and `phy-handle` values. Save the exact DTB SHA-256.
- [ ] Compare both ports in `kernel/comps/dwmac/src/arch/riscv.rs` and the RockOS DT: GMAC-1 is `0x50410000`, IRQ 70, clocks `[3,550,3,551,3,553]`, reset `[20,7,0x02000000]`, HSP offsets `0x200/0x208`, RGMII `0x294`, and reset GPIO port 29 pin 16. Confirm each value against the DT rather than relying on the Rust constants alone.
- [ ] Collect a read-only RockOS baseline for clock/reset status, pinctrl/RGMII state, and GMAC-1 link state. Do not write any register and do not reset the board for this inventory.
- [ ] Trace the vendor Linux probe/enable order from the RockOS kernel sources or boot logs. The required order must identify clock enable, HSP selection/request, RGMII/pinctrl setup, TX clock gate, reset pulse, and only then GMAC register access.
- [ ] Write `docs/porting/evidence/2026-10-07-gmac1-platform-contract.md` containing the source locations, register offsets, masks, read-only baseline values, and the single hypothesis: Asterinas currently reads the gated block before its vendor platform glue is enabled.

Exit condition: every register to be read or written by the preflight has a source-backed offset, mask, access width, and reset value. If any item is unknown, stop at QEMU and leave physical GMAC-1 locked.

## Phase 2: Define a fail-closed Asterinas probe

**Purpose:** Make the unsafe operation impossible in the known-bad state.

- [ ] Add a `PlatformReadiness` state in `kernel/comps/dwmac/src/arch/riscv.rs` with explicit states `Unknown`, `Ready`, and `NotReady`; keep `Unknown` from falling through to a GMAC read.
- [ ] Refactor the current `probe_read_only` into three functions with these contracts:
  - `discover_configs()`: DT/resource validation only; no MMIO.
  - `check_platform_readiness(config)`: acquire only the HSP/SYSCRG/GPIO/PINCTRL windows proven in Phase 1, read status/identity values, and return `Ready` only on exact expected values.
  - `probe_gmac_registers(config)`: read exactly `GMAC_PROBE_OFFSETS`; it is callable only with `Ready`.
- [ ] Emit ordered markers before every risky boundary:

  ```text
  ASTERINAS_PROBE_PLATFORM_CHECK device=gmac-1
  ASTERINAS_PROBE_PLATFORM_READY device=gmac-1
  ASTERINAS_PROBE_PHASE device=gmac-1 phase=mmio-read
  ```

  On an unknown or mismatched platform state, emit `ASTERINAS_PROBE_FAIL device=gmac-1 reason=platform-not-ready` and `ASTERINAS_PROBE_DONE ... status=fail` without acquiring or reading `0x50410000`.
- [ ] Keep the probe write-free in this phase. Do not initialize PHY/MDIO, allocate DMA rings, enable IRQs, configure TX/RX, register `eth0`, or access network backends.
- [ ] If the architecture offers a documented bus-fault handler for this MMIO window, add it only as a secondary diagnostic. Do not claim that a loop counter or software deadline protects a blocked load; the preflight gate remains mandatory.
- [ ] Add unit tests with a fake `PlatformRegisters` proving that `NotReady` performs zero GMAC reads and that the six-offset whitelist is unchanged.

Expected result: the known gated case exits deterministically as `platform-not-ready`, and no code path can reach the first GMAC read from `Unknown`.

## Phase 3: Make QEMU reproduce both unsafe and corrected states

**Purpose:** Prove the contract without risking the board.

- [ ] In `hw/net/eic7700-gmac.c`, keep GMAC-1 initially gated. A gated read must return `0xffffffff` immediately, writes must be ignored, and no PHY, DMA, TX/RX, IRQ, or network counters may change. QEMU must never block its vCPU thread to imitate the physical hang.
- [ ] In `hw/misc/eic7700.c`, track per-port platform bits for the source-backed sequence: common clocks/config, HSP PHY/AXI, TX clock clear/set, RGMII delay values, reset assert, and reset release. Expose readiness to the corresponding GMAC device only after all bits match.
- [ ] Preserve reset isolation: GMAC-1 reset changes must not clear GMAC-0’s released bit; GMAC-0 must remain readable throughout GMAC-1 transitions.
- [ ] Add qtests to `/home/ubuntu/.codex/qemu-eic7700-10.2.1/tests/qtest/eic7700-dwc3-test.c` for:
  1. 256 gated GMAC-1 reads returning immediately with `0xffffffff`;
  2. a gated write leaving the register and all side-effect counters unchanged;
  3. GMAC-0 remaining readable after GMAC-1 reset/assert operations;
  4. incomplete platform sequences keeping GMAC-1 gated;
  5. the exact source-backed sequence making GMAC-1 readable;
  6. six-register read-only access producing no PHY/DMA/TX/RX/IRQ/network side effect;
  7. reset returning both ports to their initial gate state.
- [ ] Build QEMU and run its complete EIC7700 qtest target plus the existing GMAC descriptor/PHY tests. Save the binary hash, command line, test count, and receipt under `target/eic7700-qemu/gmac1-platform-gate/`.

Exit condition: all seven gate tests pass and the model distinguishes `platform-not-ready` from a successful six-register read. A QEMU pass does not yet authorize a physical run until Phase 4 passes.

## Phase 4: Exercise the Asterinas guest in QEMU

**Purpose:** Validate marker ordering, watchdog recovery, and the absence of side effects in the real guest code.

- [ ] Boot the gated model with `asterinas.hardware_probe=gmac-1`. Expected markers are `PLATFORM_CHECK`, `FAIL reason=platform-not-ready`, and `DONE status=fail`; there must be no `MMIO_READ`, `PHY`, `MDIO`, `DMA`, `TX`, `RX`, or network marker.
- [ ] Boot the same model with the minimal platform-enable test path. Expected markers are `PLATFORM_READY`, exactly six `ATTEMPT`/`READ` pairs at the existing whitelist, `PASS`, and `DONE status=pass`.
- [ ] Arm the existing software/hardware watchdog recovery path and verify a fresh OpenSBI/U-Boot epoch after both the fail-closed and successful guest modes. Classify any run ending before a fresh epoch as initialization-only, never as recovery success.
- [ ] Run the two QEMU modes three times each. This repetition is confined to QEMU and exists to catch ordering/state leakage; it is not a license to repeat physical attempts.
- [ ] Validate every transcript with `tools/riscv/megrez_hardware_probe.py` and add parser tests for the new markers and failure class.

Expected result: QEMU proves the exact guest path that will be used on the board, including recovery, while remaining free of network traffic.

## Phase 5: Implement the minimal physical platform glue

**Purpose:** Enable only what is required to make GMAC-1’s register block readable.

- [ ] Implement the source-backed sequence in a small helper in `kernel/comps/dwmac/src/arch/riscv.rs` (split into a new module only if the file-level unit tests become unwieldy): map HSP/SYSCRG/GPIO/PINCTRL/RGMII windows, validate their ranges, and keep all accesses 32-bit and aligned.
- [ ] Apply the sequence in this order, with readback and bounded polling where the hardware exposes a status bit: configure reset GPIO/pinctrl; enable common HSP clock/config; select HSP PHY and AXI request; program RGMII selection/delays; clear and set the GMAC-1 TX clock gate; assert then release only GMAC-1’s reset bit; verify platform-ready status; read the six fixed GMAC registers.
- [ ] Preserve unrelated reset bits with read-modify-write. Make the sequence idempotent and fail closed on any readback mismatch. Do not call MDIO, PHY training, DMA setup, IRQ registration, queue creation, or normal `device::register`.
- [ ] Add fake-register tests for ordering, per-port masks, reset-bit preservation, repeated invocation, and no access outside the approved windows/offsets.
- [ ] Keep GMAC-0 and GMAC-1 as separate boot selections. A GMAC-1 failure must not fall through to GMAC-0 or normal network initialization.

Exit condition: the implementation passes all host unit tests and the QEMU guest sequence without changing the normal GMAC data path.

## Phase 6: Pre-board artifact and control audit

**Purpose:** Ensure the one physical attempt is reproducible and recoverable.

- [ ] Build the exact RISC-V kernel/initramfs and record their SHA-256, DTB SHA-256, boot arguments, selected menu entry, watchdog interval, and serial device.
- [ ] Run `git diff --check`, the focused Rust tests, Python contract tests, QEMU qtests, and the runner dry-run. Resolve the current menu/renderer mismatch by generating a matching immutable candidate; do not permanently monkeypatch manifest validation.
- [ ] Confirm the board runner sees one serial owner, a fresh U-Boot prompt, a nonce-framed RockOS UID/boot-id check, and a recovery observer before it can send the GMAC-1 selection.
- [ ] Add a hard gate that rejects a physical run when the previous receipt lacks a fresh recovery epoch, when a menu hash does not match the kernel bundle, or when the selected mode is not exactly `gmac-1`.

Expected result: the run can be audited from one manifest and cannot silently select the wrong entry or retry after a timeout.

## Phase 7: One watchdog-protected physical GMAC-1 attempt

**Purpose:** Validate the corrected probe while preserving board control.

- [ ] From RockOS, verify the serial device has one owner, collect a fresh nonce/UID/boot-id, and authenticate the software reboot only through the existing documented path. Do not change persistent U-Boot configuration.
- [ ] In the new U-Boot epoch, select only the immutable `gmac-1` Probe entry and arm the hardware watchdog. Do not issue any second command after selection.
- [ ] Accept only a complete sequence of `PLATFORM_CHECK`, `PLATFORM_READY`, six read pairs, `PASS`, and `DONE`, followed by a fresh firmware epoch and authenticated RockOS UID/boot-id recovery over a reopened serial channel and SSH.
- [ ] On `platform-not-ready`, any missing marker, any MMIO timeout, or any missed recovery observation, stop immediately, preserve the serial transcript, and classify the run. Do not retry, start PHY diagnostics, enable networking, or ask the board to perform another reset.

The physical pass criterion is therefore “six registers and recovery,” not “link up” or “network traffic.”

## Phase 8: Only after the read-only pass

- [ ] Run one separate `asterinas.gmac_diagnostic=phy` cycle using the same recovery gates. This may program only the verified platform glue and perform bounded Clause-22 reads; it must not register the network data path.
- [ ] If and only if PHY diagnostics pass, run one normal GMAC-1 registration cycle with no external traffic first. Verify IRQ, DMA ring creation, and clean shutdown separately.
- [ ] Treat external ping/TCP/UDP and desktop use as later phases with independent receipts. A QEMU descriptor loopback or host NetClient result never counts as physical-board network validation.

## Verification commands and evidence

Run these commands at the relevant phase boundaries:

```bash
python3 -m pytest tools/riscv/tests/test_megrez_hardware_probe.py \
  tools/riscv/tests/test_megrez_gmac_gate.py -q
git diff --check
```

For QEMU, run the built EIC7700 qtest target and the focused GMAC gate test, then save stdout/stderr and SHA-256 under `target/eic7700-qemu/gmac1-platform-gate/`. For the board, use the existing single-owner runner and save `serial.log`, `result.json`, artifact hashes, and the recovery attestation under a new timestamped directory. Credentials must remain outside all logs and receipts.

## Estimate and stopping rules

- Contract inventory and source comparison: 0.5–1 working day, roughly 100–200 lines of tests/evidence tooling.
- QEMU gated-platform model and qtests: 1–2 working days, roughly 300–600 C lines and 100–200 test lines.
- Asterinas preflight and minimal platform glue: 2–4 working days, roughly 250–500 Rust lines plus 100–200 host tests.
- Artifact audit and one physical attempt: about 1 working day, mostly runner/evidence work.

The expected total is 4–7 working days if the vendor clock/reset sequence is confirmed in existing sources. If the EIC7700 bus requires a hardware fault-containment mechanism that Asterinas does not currently expose, the scope becomes 1–2 weeks and the safe deliverable is the fail-closed probe plus a documented platform prerequisite; do not compensate by issuing more physical reads.

The immediate next milestone is **Phase 2 + Phase 3: fail-closed Asterinas preflight and reproducible QEMU gated-port tests**. It is the smallest change that directly addresses the observed hang while preserving a recoverable board.
