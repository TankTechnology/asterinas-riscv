# Megrez SD High Speed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Negotiate SD High Speed correctly on Megrez, run the card at 50 MHz only after CMD6 acceptance, retain a bounded 25 MHz recovery path, and measure its effect on Firefox.

**Architecture:** Extend the generic MMC command model with explicit data block sizes, then keep SCR/CSD/CMD6 protocol decisions in the hardware-independent card layer. The EIC7700 adapter owns SDHCI timing bits and clocks. Boot policy, stable mode logs, evidence classification, QEMU tests, and recovery-armed physical tests make every selected or fallback state observable.

**Tech Stack:** Safe Rust kernel component, SD/SDHCI protocol, Asterinas KTest on RISC-V QEMU, Python `unittest` evidence gates, persistent Docker build container, Megrez serial automation.

---

### Task 1: Represent non-sector SD data commands

**Files:**
- Modify: `kernel/comps/mmc/src/sdhci.rs`
- Modify: `kernel/comps/mmc/src/arch/riscv.rs`

- [ ] **Step 1: Write failing command-shape tests**

Add KTests requiring `Command::send_scr()` to encode ACMD51 as one 8-byte read,
and `Command::switch_function(argument)` to encode CMD6 as one 64-byte read.
Also require all sector constructors to remain 512-byte commands and require
`SdmaTransfer::new` to reject both metadata commands.

```rust
let scr = Command::send_scr();
assert_eq!((scr.index, scr.block_size(), scr.block_count()), (51, 8, 1));
assert_eq!(scr.data, Some(DataDirection::Read));

let switch = Command::switch_function(0x80ff_fff1);
assert_eq!((switch.index, switch.block_size(), switch.block_count()), (6, 64, 1));
assert_eq!(
    SdmaTransfer::new(switch, 0xfff0_0000..0xfff0_0040),
    Err(HostError::Unsupported),
);
```

- [ ] **Step 2: Run the focused KTest and verify RED**

Build `aster-mmc` KTests for `riscv64` with the persistent container and run the
new command test in QEMU. Expected result: compile failure because `send_scr`,
`switch_function`, and `block_size` do not exist.

- [ ] **Step 3: Implement explicit checked data shape**

Store `block_size: u16` beside `block_count` in `Command`. Keep `new` for
non-data commands, route 512-byte constructors through a private
`new_data(index, argument, direction, block_size, block_count)`, and add the two
metadata constructors. Replace `has_valid_block_count` with a validation that
requires both size and count to be zero for no-data commands and nonzero for
data commands.

Change `MmioHost::command` to program `command.block_size()` instead of literal
512. Require `SdmaTransfer::new` to accept only block size 512 and calculate its
byte count from the checked command shape.

- [ ] **Step 4: Run the focused KTests and verify GREEN**

Expected result: the new command-shape test and the existing SDHCI tests pass.

- [ ] **Step 5: Commit the command model**

```bash
git add kernel/comps/mmc/src/sdhci.rs kernel/comps/mmc/src/arch/riscv.rs
git commit -m "Support SD metadata data commands"
```

### Task 2: Parse card capabilities and switch status

**Files:**
- Modify: `kernel/comps/mmc/src/card.rs`

- [ ] **Step 1: Write failing parser tests**

Add pure tests requiring:

```rust
assert!(Csd::parse(csd_with_command_classes(1 << 10)).unwrap().supports_switch());
assert_eq!(Scr::parse(scr_with_spec(1)).unwrap().spec(), SdSpec::V1_10);
assert!(SwitchStatus::parse(status_with_hs_support()).supports_high_speed());
assert_eq!(
    SwitchStatus::parse(status_with_selected_function(1)).selected_access_mode(),
    1,
);
```

Cover CSD structure mismatch, SCR structure mismatch, SD 1.0 cards, missing
High Speed support, and selected-function values 0, 1, and 15.

- [ ] **Step 2: Run the parser KTests and verify RED**

Expected result: compile failure because the parser types are absent.

- [ ] **Step 3: Implement the minimal parsers**

Replace `csd_v2_nr_sectors` with a private `Csd` value that retains capacity
and command classes. Add an `Scr` parser for the two big-endian 32-bit words and
a borrowed `SwitchStatus` parser over exactly 64 bytes. Use named constants for
CSD `CCC_SWITCH`, SCR structure/version fields, CMD6 support byte 13, and
selected-function byte 16. Reject malformed structures and avoid storing fields
not used by this milestone.

- [ ] **Step 4: Run parser tests and verify GREEN**

Expected result: all parser cases pass with no hardware adapter involved.

- [ ] **Step 5: Commit the parsers**

```bash
git add kernel/comps/mmc/src/card.rs
git commit -m "Parse SD high-speed capabilities"
```

### Task 3: Negotiate High Speed with bounded recovery

**Files:**
- Modify: `kernel/comps/mmc/src/card.rs`
- Modify: `kernel/comps/mmc/src/sdhci.rs`
- Modify: `kernel/comps/mmc/src/arch/riscv.rs`

- [ ] **Step 1: Write failing fake-host traces**

Extend `FakeHost` so discovery traces can include 8-byte and 64-byte PIO input,
host timing changes, and one reset/restart. Add separate tests for:

```text
supported: baseline -> SCR -> CMD6 check -> CMD6 switch(selected=1)
           -> host HighSpeed -> 50 MHz
unsupported: baseline -> SCR/CMD6 check(no support) -> 25 MHz
rejected: baseline -> CMD6 switch(selected=0) -> 25 MHz
pre-switch error: check-mode data error -> 25 MHz
post-switch error: ambiguous switch transfer -> clear timing -> one baseline restart
recovery failure: ambiguous switch transfer -> one baseline restart error
```

Each test must assert the exact remaining trace is empty, the selected
`CardTiming`, and that no path retries more than once.

- [ ] **Step 2: Run fake-host tests and verify RED**

Expected result: failures show discovery still ends unconditionally at 25 MHz.

- [ ] **Step 3: Implement metadata reads and timing contract**

Add `CardTiming::{DefaultSpeed, HighSpeed}`, `Card::timing`, and
`Card::data_clock_hz`. Add `HostController::set_timing(CardTiming)`.

Implement one bounded helper that submits an 8- or 64-byte PIO read, drains
words in little-endian host-register order into the wire-order byte buffer,
waits for transfer completion, and resets the data line on error. Use it for
ACMD51 and CMD6. CMD6 check argument is `0x00ff_fff1`; CMD6 switch argument is
`0x80ff_fff1`.

Split baseline discovery from optional promotion. Promotion is attempted only
when SCR, CSD, and check status all allow it. A switch result selecting 0 stays
at default speed. An error after issuing switch is ambiguous and triggers
exactly one fresh baseline discovery after restoring host default timing.

- [ ] **Step 4: Implement SDHCI timing selection**

Map `DefaultSpeed` to Host Control High Speed Enable clear and `HighSpeed` to
bit 2 set. Keep the existing bus-width bit intact. Apply the timing bit before
requesting the selected clock and expose no register details to `card.rs`.

- [ ] **Step 5: Run all MMC KTests and verify GREEN**

Expected result: every new exact trace and all existing read/write/SDMA tests
pass on four-hart RISC-V QEMU.

- [ ] **Step 6: Commit protocol negotiation**

```bash
git add kernel/comps/mmc/src/card.rs kernel/comps/mmc/src/sdhci.rs \
  kernel/comps/mmc/src/arch/riscv.rs
git commit -m "Negotiate SD High Speed on Megrez"
```

### Task 4: Add boot policy and stable evidence

**Files:**
- Modify: `kernel/comps/mmc/src/lib.rs`
- Modify: `kernel/comps/mmc/src/arch/riscv.rs`
- Modify: `tools/riscv/megrez_sdhci_gate.py`
- Modify: `tools/riscv/tests/test_megrez_sdhci_gate.py`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Write failing policy and classifier tests**

Add a KTest requiring the absent flag to enable negotiation and the present
flag to force default speed. Add Python cases requiring exactly one ordered:

```text
[mmc] timing=high-speed clock=50000000
```

Reject duplicate timing, any `default-speed-*` timing in the High Speed gate,
or timing appearing before the card record. Keep all existing fatal, SDMA,
capacity, block-registration, CRC, and ordering checks.

- [ ] **Step 2: Run focused tests and verify RED**

Run the MMC KTest and:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_sdhci_gate -v
```

Expected result: missing flag and timing marker behavior fail.

- [ ] **Step 3: Implement policy and one-shot logs**

Define `asterinas.mmc_default_speed` as an `AtomicBool`, pass its value into
probe/discovery, and skip optional promotion when set. Emit exactly one timing
record after successful discovery:

```text
[mmc] timing=high-speed clock=50000000
[mmc] timing=default-speed-forced clock=25000000
[mmc] timing=default-speed-unsupported clock=25000000
[mmc] timing=default-speed-recovered clock=25000000
```

Extend the Python gate and README with the new ordered High Speed requirement
and document the recovery flag.

- [ ] **Step 4: Run focused tests and verify GREEN**

Expected result: MMC policy test and every Python gate test pass.

- [ ] **Step 5: Commit observability and policy**

```bash
git add kernel/comps/mmc/src/lib.rs kernel/comps/mmc/src/arch/riscv.rs \
  tools/riscv/megrez_sdhci_gate.py \
  tools/riscv/tests/test_megrez_sdhci_gate.py tools/riscv/README.md
git commit -m "Gate Megrez SD High Speed evidence"
```

### Task 5: Run local and QEMU verification

**Files:**
- Modify if needed: files from Tasks 1-4 only

- [ ] **Step 1: Format and run source checks**

```bash
cargo fmt --all -- --check
git diff --check
python3 -m unittest tools.riscv.tests.test_megrez_sdhci_gate -v
```

Expected result: all commands exit zero.

- [ ] **Step 2: Run complete MMC KTests on RISC-V QEMU**

Use the persistent development container to build `aster-mmc` tests for
`riscv64`, then boot the generated image with four harts. Expected result: all
MMC card, command, SDHCI, timing, fallback, read/write, and SDMA tests pass.

- [ ] **Step 3: Build the release kernel offline**

```bash
tools/docker/run_dev_container.sh --workspace "$PWD" -- \
  env CARGO_NET_OFFLINE=true make --old-file=/root/.cargo/bin/cargo-osdk \
  kernel CARGO_OSDK=/root/.cargo/bin/cargo-osdk TARGET_ARCH=riscv64 SMP=4 \
  FEATURES=riscv_sv39_mode RELEASE=1
```

Expected result: zero network downloads and a new release Image. Record its
size and SHA-256.

- [ ] **Step 4: Update implementation evidence**

Create `docs/porting/evidence/2026-09-13-megrez-sd-high-speed.md` containing
the exact test commands, result counts, release hash, and any unrelated suite
limitations. Do not claim physical completion here.

### Task 6: Run recovery-armed physical acceptance

**Files:**
- Modify: `docs/porting/evidence/2026-09-13-megrez-sd-high-speed.md`
- Create ignored artifacts under: `target/firefox-current-performance/`

- [ ] **Step 1: Stage only the release Image and ephemeral config**

Copy the hash-named Image through RockOS over SSH. Reuse the existing immutable
stage1, DTB, desktop rootfs, and selectable menu. Verify every staged hash and
do not overwrite partition 2 or either persistent selector.

- [ ] **Step 2: Run the light timing/CRC gate**

Boot with automatic RockOS recovery armed. Capture bounded dmesg and serial
evidence, require SDMA plus `timing=high-speed clock=50000000`, and repeat the
32 MiB CRC read. Abort desktop testing on any timeout, fallback, CRC mismatch,
or MMC error.

- [ ] **Step 3: Run matched storage benchmarks**

Read 128 MiB of `/usr/lib/firefox-esr/libxul.so` once with direct I/O and once
through the buffered path. Preserve structured start/end/byte markers. Require
direct throughput at least 13.22 MB/s, which is 35 percent above the measured
9.79 MB/s baseline.

- [ ] **Step 4: Run two cold Firefox boots**

For each run, retain menu-to-console and first-visible-window latency, Firefox
service state and restart count, bounded dmesg, serial hash, and automatic
RockOS recovery evidence. Report both values and their mean without imposing a
hard Firefox threshold.

- [ ] **Step 5: Verify recovery and persistent state**

After every run, require a new RockOS boot ID and re-check the SHA-256 of
`/boot/extlinux/asterinas.conf` and `/boot/extlinux/extlinux.conf` against the
known selectors.

- [ ] **Step 6: Publish evidence and final commit**

Update the evidence document with artifact hashes and measured comparisons,
then run `git diff --check`, inspect the complete branch diff, and commit only
the evidence/doc changes. Do not push without explicit user authorization.
