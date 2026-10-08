# DWMAC RX Liveness Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a host-executable Rust state model that produces a reproducible starvation counterexample for the current Megrez DWMAC RX protocol and verifies a finite-budget protocol before any production driver edit.

**Architecture:** A standalone pure-Rust model under `tools/riscv` represents reduced RX descriptors, DMA completion, PLIC masking, softirq scheduling, poll progress, and clear/rearm phases. A Python unittest compiles the model with the pinned Rust toolchain, exercises its stable JSON CLI, and keeps the model in the normal host gate. A separate evidence note binds model assumptions to pinned Linux `stmmac` source; production driver changes are deliberately deferred to a follow-up plan derived from the counterexample.

**Tech Stack:** Rust 2024 standard library, Python `unittest`, repository Makefile, Linux/ESWIN `stmmac` primary sources.

---

## File map

- Create `tools/riscv/dwmac_rx_liveness_model.rs`: immutable model state, transition relation, reachable-state exploration, shortest starvation-lasso search, invariant checks, stable JSON CLI, and Rust unit tests.
- Create `tools/riscv/tests/test_dwmac_rx_liveness_model.py`: compile the standalone Rust source once and test the public CLI without Docker, QEMU, network access, or a board.
- Modify `Makefile`: add one focused host target that runs the Python model tests.
- Create `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`: record exact Linux/ESWIN source identities, verified register/scheduling rules, and explicit remaining hardware assumptions.

This plan intentionally stops before modifying `kernel/comps/dwmac`,
`kernel/libs/aster-bigtcp`, or the softirq implementation. The model result is
the input to a separate production-fix plan, so the fix is not guessed in
advance.

### Task 1: Freeze the standalone model CLI and state vocabulary

**Files:**
- Create: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`
- Create: `tools/riscv/dwmac_rx_liveness_model.rs`

- [ ] **Step 1: Write the failing public-CLI tests**

Create `tools/riscv/tests/test_dwmac_rx_liveness_model.py` with a class-level
temporary directory and one compilation per test process:

```python
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_SOURCE = REPOSITORY_ROOT / "tools/riscv/dwmac_rx_liveness_model.rs"


class DwmacRxLivenessModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls._temporary_directory.name) / "dwmac-rx-model"
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                "-Dwarnings",
                str(MODEL_SOURCE),
                "-o",
                str(cls.binary),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def run_model(self, protocol: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), "--protocol", protocol, "--ring-size", "2", "--json"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_current_protocol_reports_starvation_counterexample(self) -> None:
        result = self.run_model("current")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "current")
        self.assertEqual(report["verdict"], "counterexample")
        self.assertEqual(report["property"], "bounded-rx-poll")
        self.assertGreater(len(report["prefix"]), 0)
        self.assertIn("dma-complete", report["cycle"])
        self.assertIn("poll-consume", report["cycle"])

    def test_cli_rejects_noncanonical_arguments(self) -> None:
        for arguments in (
            [],
            ["--protocol", "unknown", "--ring-size", "2", "--json"],
            ["--protocol", "current", "--ring-size", "1", "--json"],
            ["--protocol", "current", "--ring-size", "5", "--json"],
            ["--protocol", "current", "--ring-size", "02", "--json"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(self.binary), *arguments],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and record the missing-source RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model -v
```

Expected: exit nonzero because `tools/riscv/dwmac_rx_liveness_model.rs` does
not exist. Existing repository files must not be modified to manufacture this
failure.

- [ ] **Step 3: Add the minimal model vocabulary and strict CLI**

Create `tools/riscv/dwmac_rx_liveness_model.rs`. Define the model types before
the transition relation so later tasks cannot silently change their meaning:

```rust
#![allow(dead_code)]

use std::{env, process::ExitCode};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Owner {
    Dma,
    CpuComplete,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum EndPhase {
    None,
    ClearStatus,
    Rearm,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Protocol {
    Current,
    Bounded,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Action {
    DmaComplete,
    DeliverIrq,
    StartRxPoll,
    PollConsume,
    PollFinishEmpty,
    PollFinishBudget,
    ClearStatus,
    Rearm,
    ServiceTx,
    ServiceTimer,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct State {
    owners: Vec<Owner>,
    dma_cursor: usize,
    rx_head: usize,
    tail: usize,
    irq_masked: bool,
    irq_asserted: bool,
    rx_pending: bool,
    tx_pending: bool,
    timer_pending: bool,
    polling: bool,
    budget_left: u8,
    end_phase: EndPhase,
}

#[derive(Debug)]
struct Report {
    protocol: Protocol,
    explored_states: usize,
    prefix: Vec<Action>,
    cycle: Vec<Action>,
}

fn parse_args() -> Result<(Protocol, usize), String> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    if arguments.len() != 5
        || arguments[0] != "--protocol"
        || arguments[2] != "--ring-size"
        || arguments[4] != "--json"
    {
        return Err("usage: dwmac-rx-model --protocol current|bounded --ring-size 2|3|4 --json".into());
    }
    let protocol = match arguments[1].as_str() {
        "current" => Protocol::Current,
        "bounded" => Protocol::Bounded,
        _ => return Err("usage: dwmac-rx-model --protocol current|bounded --ring-size 2|3|4 --json".into()),
    };
    let ring_size = match arguments[3].as_str() {
        "2" => 2,
        "3" => 3,
        "4" => 4,
        _ => return Err("usage: dwmac-rx-model --protocol current|bounded --ring-size 2|3|4 --json".into()),
    };
    Ok((protocol, ring_size))
}
```

Add `Action::name`, stable JSON string escaping for the fixed action names,
and a `main` that returns 2 for CLI errors. Leave model evaluation returning
an explicit internal error so the first behavior test remains RED rather than
being faked:

```rust
fn main() -> ExitCode {
    let (protocol, ring_size) = match parse_args() {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::from(2);
        }
    };
    let _ = (protocol, ring_size);
    eprintln!("model transition relation is not implemented");
    ExitCode::from(3)
}
```

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2. Expected: the argument-rejection test passes;
the current-protocol test fails with return code 3 instead of 1.

- [ ] **Step 5: Commit the frozen boundary**

```bash
git add tools/riscv/dwmac_rx_liveness_model.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(riscv): define DWMAC RX liveness model"
```

### Task 2: Model the current protocol and emit its shortest starvation lasso

**Files:**
- Modify: `tools/riscv/dwmac_rx_liveness_model.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Add a RED assertion for a stable minimal trace**

Extend `test_current_protocol_reports_starvation_counterexample`:

```python
self.assertLessEqual(len(report["prefix"]) + len(report["cycle"]), 12)
self.assertEqual(report["cycle"].count("dma-complete"), 2)
self.assertEqual(report["cycle"].count("poll-consume"), 2)
self.assertNotIn("service-timer", report["cycle"])
self.assertNotIn("service-tx", report["cycle"])
```

Run the focused unittest. Expected: it still fails because the model exits 3.

- [ ] **Step 2: Implement initial state and the current transition relation**

Add these exact entry points:

```rust
fn initial_state(ring_size: usize, protocol: Protocol) -> State;
fn successors(state: &State, protocol: Protocol) -> Vec<(Action, State)>;
fn validate_state(state: &State) -> Result<(), &'static str>;
fn reachable_graph(
    initial: State,
    protocol: Protocol,
) -> Result<HashMap<State, Vec<(Action, State)>>, &'static str>;
fn shortest_starvation_lasso(
    graph: &HashMap<State, Vec<(Action, State)>>,
    initial: &State,
) -> Option<(Vec<Action>, Vec<Action>)>;
```

`initial_state` uses all-DMA-owned descriptors, head/cursor/tail zero, an
unmasked/deasserted IRQ, no RX/TX work, a pending timer obligation, no active
poll, and a zero budget for `Current`.

Remove the temporary crate-level `#![allow(dead_code)]`, add
`collections::HashMap` to the imports, and require `rustc -Dwarnings` to prove
that every frozen type is now exercised by the model or report path.

`successors` implements only enabled actions. In particular:

```rust
if state.polling && state.owners[state.rx_head] == Owner::CpuComplete {
    let mut next = state.clone();
    next.owners[next.rx_head] = Owner::Dma;
    next.rx_head = (next.rx_head + 1) % next.owners.len();
    next.tail = next.rx_head;
    result.push((Action::PollConsume, next));
}
if state.polling && state.owners[state.rx_head] == Owner::Dma {
    let mut next = state.clone();
    next.polling = false;
    next.end_phase = EndPhase::ClearStatus;
    result.push((Action::PollFinishEmpty, next));
}
```

DMA completion may occur whenever `owners[dma_cursor] == Owner::Dma`; it makes
that descriptor CPU-complete, advances `dma_cursor`, and asserts the DMA/PLIC
level. IRQ delivery requires asserted and unmasked, masks the source, clears
the delivered level at the PLIC boundary, and marks RX and TX pending. Starting
RX polling consumes `rx_pending`. Current polling has no budget transition.
TX/timer actions are enabled only while no callback poll/end phase is active,
which represents their inability to preempt the running RX softirq callback.

`reachable_graph` explores until no unseen states remain, validates every
state, and refuses more than 100,000 states. `shortest_starvation_lasso` first
uses breadth-first search for the shortest prefix to each state, then
breadth-first search from each state satisfying
`polling && timer_pending && tx_pending` back to itself. A valid cycle must
contain both `DmaComplete` and `PollConsume` and neither service action. Select
the lowest total prefix-plus-cycle length, breaking ties by action-name order.

- [ ] **Step 3: Emit the stable JSON report and exit semantics**

For a lasso, print one compact JSON object with keys in this order:

```json
{"protocol":"current","verdict":"counterexample","property":"bounded-rx-poll","explored_states":0,"prefix":[],"cycle":[]}
```

Replace the zero and arrays with the real values. Exit 1 means a property
counterexample, exit 0 means all modeled properties pass, and exit 3 means an
internal model/invariant error. Never print diagnostics to stdout.

- [ ] **Step 4: Run the current-protocol GREEN**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model.DwmacRxLivenessModelTests.test_current_protocol_reports_starvation_counterexample \
  tools.riscv.tests.test_dwmac_rx_liveness_model.DwmacRxLivenessModelTests.test_cli_rejects_noncanonical_arguments -v
```

Expected: 2 tests pass in less than 10 seconds. Save the emitted JSON from a
manual CLI invocation under the test failure message only; do not add a
generated trace file to git.

- [ ] **Step 5: Commit the current-protocol counterexample**

```bash
git add tools/riscv/dwmac_rx_liveness_model.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(riscv): expose unbounded DWMAC RX poll"
```

### Task 3: Verify the finite-budget protocol and clear/rearm race

**Files:**
- Modify: `tools/riscv/dwmac_rx_liveness_model.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Add bounded-protocol RED tests**

Add these tests:

```python
def test_bounded_protocol_has_no_starvation_or_lost_wakeup(self) -> None:
    result = self.run_model("bounded")
    self.assertEqual(result.returncode, 0, result.stderr)
    report = json.loads(result.stdout)
    self.assertEqual(report["protocol"], "bounded")
    self.assertEqual(report["verdict"], "verified-within-model")
    self.assertEqual(
        report["properties"],
        [
            "descriptor-ownership",
            "bounded-rx-poll",
            "eventual-rearm-or-reschedule",
            "no-lost-rx-wakeup",
            "tx-timer-progress",
        ],
    )
    self.assertGreater(report["explored_states"], 0)

def test_all_reduced_ring_sizes_are_verified(self) -> None:
    for ring_size in ("2", "3", "4"):
        with self.subTest(ring_size=ring_size):
            result = subprocess.run(
                [str(self.binary), "--protocol", "bounded", "--ring-size", ring_size, "--json"],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
```

Run the focused suite. Expected: both tests fail because `Bounded` currently
uses the current unbounded transition relation.

- [ ] **Step 2: Add finite budget and split poll completion into clear/rearm**

For `Protocol::Bounded`, initialize `budget_left` to the ring size when polling
starts. Every `PollConsume` decrements it. At zero, disable further consume and
enable `PollFinishBudget`, even when the head descriptor is complete.

`PollFinishBudget` ends polling, records whether any descriptor is already
CPU-complete by setting `rx_pending`, and enters `ClearStatus`. Split poll end:

```rust
if state.end_phase == EndPhase::ClearStatus {
    let mut next = state.clone();
    next.irq_asserted = false;
    next.end_phase = EndPhase::Rearm;
    result.push((Action::ClearStatus, next));
}
if state.end_phase == EndPhase::Rearm {
    let mut next = state.clone();
    next.irq_masked = false;
    next.end_phase = EndPhase::None;
    if next.owners.iter().any(|owner| *owner == Owner::CpuComplete) {
        next.rx_pending = true;
    }
    result.push((Action::Rearm, next));
}
```

Keep `DmaComplete` enabled between these two actions. This makes an arrival in
the clear/rearm window explicit and requires `Rearm` to preserve or recreate
RX work. Do not make `DmaComplete` itself directly schedule a softirq while the
PLIC source is masked.

- [ ] **Step 3: Check safety and all bounded liveness failures**

Add predicates:

```rust
fn is_lost_rx_wakeup(state: &State) -> bool;
fn has_invalid_ownership(state: &State) -> bool;
fn find_rearm_or_reschedule_cycle(
    graph: &HashMap<State, Vec<(Action, State)>>,
) -> Option<Vec<Action>>;
fn find_tx_timer_starvation_cycle(
    graph: &HashMap<State, Vec<(Action, State)>>,
) -> Option<Vec<Action>>;
```

`is_lost_rx_wakeup` is true only when at least one descriptor is CPU-complete,
no RX work is pending/running, no end phase is active, the IRQ is unmasked, and
no asserted level can cause delivery. `validate_state` rejects head/cursor/tail
outside the ring and any impossible phase/poll combination. The bounded report
exits 3 and names the first violated property if any predicate or lasso is
found; it exits 0 only when the full reachable graph contains no violation.

- [ ] **Step 4: Run GREEN for every reduced ring size**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_dwmac_rx_liveness_model -v
```

Expected: 4 tests pass, each CLI invocation completes within its 10-second
deadline, current exits 1 with a lasso, and bounded exits 0.

- [ ] **Step 5: Compile and run Rust's internal invariant tests**

Add `#[cfg(test)]` tests in the Rust source for one-step ownership, wraparound,
arrival-between-clear-and-rearm, and exact budget exhaustion. Then run:

```bash
MODEL_TEST_BIN=$(mktemp /tmp/dwmac-rx-model-test.XXXXXX)
rustc --edition=2024 -Dwarnings --test \
  tools/riscv/dwmac_rx_liveness_model.rs -o "$MODEL_TEST_BIN"
"$MODEL_TEST_BIN" --nocapture
rm -f -- "$MODEL_TEST_BIN"
```

Expected: all Rust tests pass; the temporary binary is removed.

- [ ] **Step 6: Commit the verified candidate protocol**

```bash
git add tools/riscv/dwmac_rx_liveness_model.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(riscv): verify bounded DWMAC RX progress"
```

### Task 4: Bind the model to the Linux/ESWIN hardware contract

**Files:**
- Create: `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`

- [ ] **Step 1: Capture exact primary-source identities**

Use the ESWIN Linux 6.6 branch already named by the M5 design and record exact
commit hashes rather than relying on moving web pages:

```bash
git ls-remote https://github.com/eswincomputing/linux-stable.git \
  refs/heads/linux-6.6.18-EIC7X
git ls-remote https://github.com/torvalds/linux.git refs/heads/master
```

Fetch only the needed files at the recorded commits with `curl --fail --location`
from `raw.githubusercontent.com` into a `mktemp -d` directory. Required files:

- `drivers/net/ethernet/stmicro/stmmac/stmmac_main.c`
- `drivers/net/ethernet/stmicro/stmmac/dwmac4_dma.c`
- `drivers/net/ethernet/stmicro/stmmac/dwmac4_descs.c`
- `drivers/net/ethernet/stmicro/stmmac/dwmac-win2030.c` from ESWIN

The temporary directory is diagnostic input and must be removed after the note
is written; do not vendor Linux source into Asterinas.

- [ ] **Step 2: Record only rules supported by exact source locations**

Create `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md` with this
fixed structure:

```markdown
# Megrez DWMAC RX Liveness Contract

## Source identity
## Verified RX budget and NAPI completion rules
## Verified descriptor ownership and tail-pointer rules
## Verified DMA status clear and RBU restart rules
## Mapping to the Asterinas model
## Unproved EIC7700 assumptions
## Consequence for the production-fix plan
```

Each verified rule includes repository, commit, path, function or macro name,
and a paraphrase. In particular, record Linux's finite NAPI `budget`, the
condition under which NAPI completion reenables DMA IRQs, the index passed to
`stmmac_set_queue_rx_tail_ptr`, and how RBU/status is cleared or restarted.
Do not copy more than 25 consecutive words from any source. If ESWIN delegates
to generic `stmmac`, state that boundary explicitly.

The unproved section must retain these assumptions:

- EIC7700 implements the referenced DWMAC register semantics;
- Asterinas cache synchronization matches the platform's noncoherent DMA
  requirements;
- PLIC level behavior matches the DT trigger contract;
- MMIO write ordering is sufficient at the current OSTD boundary.

- [ ] **Step 3: Self-check the evidence note**

```bash
NOTE=docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md
rg -n "TBD|TODO|FIXME|unknown commit|moving branch" "$NOTE" && exit 1 || true
rg -n "stmmac_main.c|dwmac4_dma.c|dwmac4_descs.c|dwmac-win2030.c|budget|tail|RBU|PLIC|cache" "$NOTE"
git diff --check -- "$NOTE"
```

Expected: the first search finds nothing, the second finds every required
contract topic, and diff check exits 0.

- [ ] **Step 4: Commit the hardware contract**

```bash
git add docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md
git commit -m "docs(riscv): bind DWMAC RX liveness contract"
```

### Task 5: Add the focused gate and publish the model result

**Files:**
- Modify: `Makefile`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Add a failing Make target test**

Run before editing the Makefile:

```bash
make test_riscv_dwmac_rx_model
```

Expected: nonzero with `No rule to make target 'test_riscv_dwmac_rx_model'`.

- [ ] **Step 2: Add the focused Make target**

Add beside the other RISC-V host test targets:

```make
.PHONY: test_riscv_dwmac_rx_model
test_riscv_dwmac_rx_model:
	python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_dwmac_rx_liveness_model -v
```

- [ ] **Step 3: Verify model determinism**

Extend the Python suite with:

```python
def test_current_counterexample_is_deterministic(self) -> None:
    first = self.run_model("current")
    second = self.run_model("current")
    self.assertEqual(first.returncode, 1)
    self.assertEqual(second.returncode, 1)
    self.assertEqual(first.stdout, second.stdout)
    self.assertEqual(first.stderr, second.stderr)
```

Run it once before stabilizing action ordering. Expected RED if `HashMap`
iteration leaks into trace selection. Make successor and candidate ordering
explicit by `Action::name()` so the repeated outputs become byte-identical.

- [ ] **Step 4: Run the final host verification exactly once**

```bash
make test_riscv_dwmac_rx_model
python3 -m py_compile tools/riscv/tests/test_dwmac_rx_liveness_model.py
rustfmt --edition 2024 --check tools/riscv/dwmac_rx_liveness_model.rs
git diff --check
git status --short
```

Expected: all model tests pass, current protocol deterministically exits 1
with a bounded-poll counterexample, bounded protocols for rings 2/3/4 exit 0,
all static checks pass, and status lists only the scoped plan files plus the
pre-existing unrelated MMC working-tree changes.

- [ ] **Step 5: Commit the gate**

```bash
git add Makefile tools/riscv/tests/test_dwmac_rx_liveness_model.py
git commit -m "test(riscv): gate DWMAC RX liveness model"
```

- [ ] **Step 6: Stop before production code and write the next plan from evidence**

Summarize the shortest current-protocol lasso, bounded reachable-state counts,
all verified Linux/ESWIN rules, and remaining hardware assumptions. The next
plan may modify production code only where that evidence points. It must retain
the one-physical-run rule and must not bundle MMC deployment, desktop, xHCI, or
browser changes.
