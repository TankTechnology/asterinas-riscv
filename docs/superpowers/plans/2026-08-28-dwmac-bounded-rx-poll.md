# DWMAC Bounded RX Poll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Megrez DWMAC receive callback yield after 32 packets, preserve masked/rescheduled work when packets remain, and rearm the PLIC source only after a race-safe drained observation.

**Architecture:** Add one dependency-free poll-budget state machine to `aster-dwmac` and compile its real production source as a host Rust test. `DwmacDevice` counts successful receives within the current iface poll. At poll completion it services DMA status, checks the descriptor again, and either keeps deferred work scheduled or rearms the PLIC source; no generic VirtIO or TCP API changes are required.

**Tech Stack:** safe Rust, standalone `rustc --test`, Asterinas ktests/OSDK compile, existing host liveness model.

---

### Task 1: Add a host-tested production poll budget

**Files:**
- Create: `kernel/comps/dwmac/src/poll.rs`
- Modify: `kernel/comps/dwmac/src/lib.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Write the missing-production-module RED**

Add a second unittest class that compiles the exact production module:

```python
POLL_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/poll.rs"


class DwmacRxPollContractTests(unittest.TestCase):
    def test_production_poll_budget_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "dwmac-rx-poll-tests"
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    "--test",
                    str(POLL_SOURCE),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary), "--nocapture"],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("4 passed", result.stdout)
```

Run only this class. Expected RED: `rustc` cannot read `poll.rs`.

- [ ] **Step 2: Implement the minimal dependency-free state machine**

Create `poll.rs` with:

```rust
// SPDX-License-Identifier: MPL-2.0

pub(crate) const RX_POLL_BUDGET: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PollEndAction {
    Rearm,
    Reschedule,
    Stop,
}

#[derive(Debug, Default)]
pub(crate) struct RxPollBudget {
    processed: usize,
}

impl RxPollBudget {
    pub(crate) fn can_receive(&self) -> bool {
        self.processed < RX_POLL_BUDGET
    }

    pub(crate) fn record_received(&mut self) {
        debug_assert!(self.can_receive());
        self.processed += 1;
    }

    pub(crate) fn finish(&mut self, fatal: bool, more_rx: bool) -> PollEndAction {
        self.processed = 0;
        if fatal {
            PollEndAction::Stop
        } else if more_rx {
            PollEndAction::Reschedule
        } else {
            PollEndAction::Rearm
        }
    }
}
```

Add four `#[cfg(test)]` host tests in the same file:

1. exactly 32 records exhaust the budget;
2. `finish(false, false)` resets and returns `Rearm`;
3. `finish(false, true)` resets and returns `Reschedule`;
4. fatal overrides remaining work and returns `Stop`.

Add `mod poll;` to `kernel/comps/dwmac/src/lib.rs`.

- [ ] **Step 3: Run focused GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model.DwmacRxPollContractTests -v
rustfmt --edition 2024 --check kernel/comps/dwmac/src/poll.rs
git diff --check
git add kernel/comps/dwmac/src/poll.rs kernel/comps/dwmac/src/lib.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(net): define bounded DWMAC RX poll"
```

### Task 2: Integrate bounded completion into the real DWMAC device

**Files:**
- Modify: `kernel/comps/dwmac/src/device.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Add a source-integration RED**

Add a host test that reads `device.rs` and freezes the three integration
points without mocking behavior:

```python
def test_device_uses_poll_budget_at_all_three_boundaries(self) -> None:
    source = (REPOSITORY_ROOT / "kernel/comps/dwmac/src/device.rs").read_text()
    self.assertIn("rx_poll: RxPollBudget", source)
    self.assertIn("self.rx_poll.can_receive()", source)
    self.assertIn("self.rx_poll.record_received()", source)
    self.assertIn("self.rx_poll.finish(self.fatal, more_rx)", source)
```

Run the test and expect all four assertions to fail before editing production.

- [ ] **Step 2: Wire the poll budget into `DwmacDevice`**

Import `PollEndAction` and `RxPollBudget`. Add `rx_poll` to `DwmacDevice` and
initialize it with `RxPollBudget::default()`.

Change the device boundaries exactly as follows:

```rust
fn can_receive(&self) -> bool {
    !self.fatal && self.rx_poll.can_receive() && self.queue.can_receive()
}

fn receive(&mut self) -> Result<RxBuffer, NetError> {
    let packet = self.queue.receive();
    // Preserve the existing RX-tail write and error mapping.
    match packet {
        Ok(buffer) => {
            self.rx_poll.record_received();
            Ok(buffer)
        }
        Err(QueueError::Allocation) => Err(NetError::NoMemory),
        Err(QueueError::NotReady) => Err(NetError::NotReady),
        Err(_) => {
            self.service_status();
            Err(NetError::NotReady)
        }
    }
}
```

At poll end, read/clear DMA status first, then check the descriptor again. This
ordering covers a completion between the last ingress empty check and status
service:

```rust
fn notify_poll_end(&mut self) {
    self.service_status();
    let more_rx = !self.fatal && self.queue.can_receive();
    match self.rx_poll.finish(self.fatal, more_rx) {
        PollEndAction::Rearm => {
            if self.irq.rearm().is_err() {
                self.fatal = true;
            }
        }
        PollEndAction::Reschedule => {
            aster_network::raise_send_softirq();
            aster_network::raise_receive_softirq();
        }
        PollEndAction::Stop => {}
    }
}
```

The `Reschedule` arm deliberately does not rearm the PLIC source. RX and TX are
both raised because DWMAC uses one shared masked source and completions may
arrive while RX is polling.

- [ ] **Step 3: Run host GREEN and commit**

```bash
make test_riscv_dwmac_rx_model
python3 -m py_compile tools/riscv/tests/test_dwmac_rx_liveness_model.py
rustfmt --edition 2024 --check \
  kernel/comps/dwmac/src/poll.rs kernel/comps/dwmac/src/device.rs
git diff --check
git add kernel/comps/dwmac/src/device.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "fix(net): bound Megrez DWMAC RX polling"
```

### Task 3: Compile and run non-hardware regression gates

**Files:**
- No production changes expected

- [ ] **Step 1: Run the complete host model gate**

```bash
make test_riscv_dwmac_rx_model
```

Expected: current abstract protocol still emits its deterministic lasso;
bounded abstract rings 2/3/4 verify; the exact production `poll.rs` reports
four passed tests; device integration assertions pass.

- [ ] **Step 2: Run the pinned RISC-V OSDK compile gate**

In the existing pinned Asterinas container environment, run:

```bash
cargo osdk check --ktests -p aster-dwmac -p aster-network \
  -p aster-kernel --target riscv64imac-unknown-none-elf
```

Expected: exit 0. This compiles both host-tested poll logic and the real
RISC-V device integration; it does not access QEMU or the board.

- [ ] **Step 3: Run existing QEMU network regression only if its artifacts are ready**

Use the existing fast Megrez/QEMU network plan without rebuilding desktop or
rootfs. If its immutable kernel/initramfs inputs are absent, record the gate as
not run rather than downloading or rebuilding unrelated artifacts. QEMU is
regression evidence for generic networking only and must not be reported as a
DWMAC hardware pass.

- [ ] **Step 4: Final static verification**

```bash
python3 -m py_compile tools/riscv/tests/test_dwmac_rx_liveness_model.py
rustfmt --edition 2024 --check \
  tools/riscv/dwmac_rx_liveness_model.rs \
  kernel/comps/dwmac/src/poll.rs kernel/comps/dwmac/src/device.rs
git diff --check
git status --short
```

Expected: all checks pass and the only unrelated modifications remain the
pre-existing MMC board-session files.

### Task 4: Prepare one physical discriminator, but do not launch it

**Files:**
- Modify only the existing Megrez network-stress plan/evidence tooling if it
  cannot already run the four ordered sizes.

- [ ] **Step 1: Freeze one-run acceptance**

The physical run must test 16 KiB, 64 KiB, 1 MiB, and 16 MiB in one boot. It
must stop at the first failure and record exact completed bytes, RX budget
exhaustions, reschedules, PLIC rearms, timer progress, panic/oops markers, and
software recovery.

- [ ] **Step 2: Stage only after all local gates pass**

Boot RockOS Linux, transfer the frozen kernel/DTB/initramfs over RJ45, verify
SHA-256, reboot to U-Boot, and load from MMC. Do not use XMODEM for unchanged
large artifacts and do not ask for repeated manual resets.

- [ ] **Step 3: Run once and classify**

One passing boot closes the unbounded-poll hypothesis. A failure with bounded
poll counters still advancing moves investigation to DMA/cache/PLIC hardware
assumptions; it does not authorize stacking another speculative driver fix.
