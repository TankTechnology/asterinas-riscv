# Megrez DWMAC High-Information Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one safe Megrez boot that locates the first-burst TCP stall at the host socket, guest socket, DWMAC RX, or DWMAC TX boundary and then observes automatic return to U-Boot.

**Architecture:** Extend the existing simulation-first Megrez workflow rather than creating a second board controller. The probe server records bounded Linux `TCP_INFO`; the board state machine treats PASS or FAIL as a terminal guest outcome and then waits for a fresh U-Boot recovery prompt; the DWMAC driver exposes logarithmically sampled counters without changing its control path. One frozen plan and one physical execution follow only after all local gates pass.

**Tech Stack:** Python 3 `unittest`, Linux TCP sockets/TCP_INFO, C initramfs probe, safe Rust DWMAC component, existing Rust liveness model, cargo-osdk RISC-V checks, QEMU generic-network/recovery gate, U-Boot serial runner.

---

## File map

- Modify `tools/riscv/megrez_debug_board.py`: terminal outcome/recovery state machine and bounded recovery classification.
- Modify `tools/riscv/tests/test_megrez_debug.py`: split-marker, stale-prompt, FAIL recovery, recovery-timeout, TCP_INFO, and publication tests.
- Modify `tools/riscv/megrez_debug_probe.py`: bounded TCP_INFO samples and canonical trace snapshot.
- Modify `tools/riscv/megrez_debug.py`: bind the probe trace to physical evidence.
- Modify `tools/riscv/debian/rootfs/megrez_tcp_probe_init.c`: report current-stage received bytes on failure.
- Modify `tools/riscv/tests/test_debian_m5_network.py`: probe failure/progress self-tests.
- Modify `kernel/comps/dwmac/src/poll.rs`: read-only public-to-component counter snapshot and diagnostic cadence.
- Modify `kernel/comps/dwmac/src/queue.rs`: TX submitted/reclaimed/outstanding and RX cursor snapshot.
- Modify `kernel/comps/dwmac/src/device.rs`: low-rate diagnostic emission at poll completion.
- Modify `tools/riscv/tests/test_dwmac_rx_liveness_model.py`: compile/source integration expectations for the diagnostic boundary.
- Modify `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`: Linux TX audit and exact experiment interpretation.
- Create a fresh plan/output directory under ignored `target/megrez-debug/`: exact artifacts and physical evidence only, never source.

The pre-existing modifications to `tools/riscv/megrez_board_session.py` and
`tools/riscv/tests/test_megrez_board_session.py` are outside this plan and must
not be staged or edited.

### Task 1: Correct terminal-marker and recovery semantics

**Files:**
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `tools/riscv/megrez_debug_board.py`

- [ ] **Step 1: Write focused failing state-machine tests**

Add tests that feed markers in small chunks through the real `run_board` seam:

```python
def test_guest_failure_waits_for_fresh_uboot_recovery(self) -> None:
    # Initial U-Boot output belongs to operations.open and is not fed to tracker.
    chunks = (
        "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_FAIL rea",
        "son=receive-poll errno=110 attempts=1 current_bytes=14600 ",
        "completed_bytes=0\npll config ok\nFirmware version:1.4\n",
        "U-Boot 2020.01\n=> ",
    )
    result = run_board(plan, BoardRunConfig(120), FakeOperations(chunks))
    self.assertFalse(result.passed)
    self.assertEqual(result.reason, "guest-failure-recovered:receive-poll")
```

Add sibling cases requiring: PASS then fresh prompt => `board-pass`; FAIL then
no prompt => `recovery-not-observed`; a prompt before the terminal marker does
not count; duplicate/out-of-order terminal markers fail closed; total deadline
causes no second `booti`.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardRunTests -v
```

Expected: new FAIL/recovery cases fail because `_MarkerTracker` only recognizes
successful final milestones and ignores `ASTERINAS_GMAC_TCP_PROBE_FAIL`.

- [ ] **Step 3: Implement a terminal-aware tracker**

Introduce one immutable terminal record and keep recovery separate:

```python
@dataclass(frozen=True)
class GuestTerminal:
    passed: bool
    reason: str

class _MarkerTracker:
    @property
    def terminal(self) -> GuestTerminal | None: ...
    def feed(self, chunk: str) -> bool:
        """Return true only for a fresh prompt observed after terminal."""
```

Match exactly one canonical probe failure marker and extract only its safe
`reason=[a-z0-9-]+`. Preserve split-marker tails. After a terminal marker,
ignore milestone parsing and wait for a prompt occurring later in the current
window or a later chunk. `run_board` publishes `board-pass` only for a passing
terminal plus recovery; a recovered failure is non-passing with
`guest-failure-recovered:<reason>`.

- [ ] **Step 4: Run GREEN and commit**

Run the focused command, then all `tools.riscv.tests.test_megrez_debug` tests,
`py_compile`, Ruff check/format, and `git diff --check`. Commit only the two
files:

```bash
git commit -m "fix(riscv): classify Megrez probe recovery"
```

### Task 2: Add bounded host TCP_INFO evidence

**Files:**
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `tools/riscv/megrez_debug_probe.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/megrez_debug_board.py`

- [ ] **Step 1: Write TCP_INFO and publication RED tests**

Use a real loopback listener/client. One client reads normally; a second uses a
small receive buffer and stops reading while the server sends a bounded body.
Freeze this API:

```python
with ProbeServer(host="127.0.0.1", port=0, payload_sizes=(16384,)) as server:
    ...
trace = server.trace_snapshot(plan_sha256="0" * 64)
self.assertEqual(trace["schema_version"], 1)
self.assertLessEqual(len(trace["connections"][0]["samples"]), 4096)
self.assertIn("bytes_acked", trace["connections"][0]["samples"][-1])
```

Require strictly nondecreasing sample timestamps/bytes-sent/bytes-acked,
bounded sample count, explicit truncation, peer/request identity, application
bytes accepted, socket outcome, and canonical JSON. A physical-workflow test
must require `probe-tcp-info.json` in the board evidence set and reject a trace
whose plan hash differs.

- [ ] **Step 2: Run RED**

Run only `MegrezDebugProbeServerTests` and the new publication test. Expected:
failure because `trace_snapshot` and evidence-provider plumbing do not exist.

- [ ] **Step 3: Implement sampling without raw-packet privilege**

Define fixed dataclasses for `TcpInfoSample` and one connection trace. Parse a
local ctypes representation matching `/usr/include/linux/tcp.h`; assert the
minimum buffer size before reading fields. Start one bounded sampler thread per
accepted socket at 20 ms cadence, stop/join it in every `_handle` exit, and cap
at 4096 samples. Sampling errors become an explicit trace outcome rather than
terminating the responder.

Add an optional evidence provider to `RealBoardOperations`. During its existing
descriptor-pinned `publish`, write canonical `probe-tcp-info.json` and include
it in the `StageResult.evidence` tuple. `megrez_debug.main` passes the live
server's snapshot provider; simulation factories remain unchanged.

- [ ] **Step 4: Run GREEN and commit**

Run all Megrez debug tests three times to catch thread/socket leaks, then
py_compile, Ruff, diff-check, and a process/socket residue check. Commit:

```bash
git commit -m "test(riscv): capture Megrez TCP progress"
```

### Task 3: Make guest failure quantitative

**Files:**
- Modify: `tools/riscv/debian/rootfs/megrez_tcp_probe_init.c`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] **Step 1: Write native RED cases**

Extend the self-test to feed a response in multiple chunks, stop after 14,600
body bytes, and require the failure line:

```text
ASTERINAS_GMAC_TCP_PROBE_FAIL reason=receive-poll errno=110 attempts=1 current_bytes=14600 completed_bytes=0
```

Also require `current_bytes=0` for connect/send failures and reset to zero at
the start of every ordered stage.

- [ ] **Step 2: Run RED**

Run the two native probe unittest methods. Expected: marker mismatch because
the production failure struct records only completed stages.

- [ ] **Step 3: Implement the minimum counter**

Add `current_bytes` to `probe_failure`. Update it only after
`response_stream_consume` accepts bytes belonging to the body; do not count
headers. Emit the field in the single terminal failure line. Preserve exact
payload validation and terminal hold semantics.

- [ ] **Step 4: Run GREEN and commit**

Compile both self-test and production branches with
`-Wall -Wextra -Werror`; run the focused class, py_compile/Ruff/diff-check, and
commit:

```bash
git commit -m "test(riscv): report stalled Megrez receive bytes"
```

### Task 4: Audit and instrument DWMAC RX/TX progress

**Files:**
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`
- Modify: `kernel/comps/dwmac/src/poll.rs`
- Modify: `kernel/comps/dwmac/src/queue.rs`
- Modify: `kernel/comps/dwmac/src/device.rs`
- Modify: `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`

- [ ] **Step 1: Complete the pinned Linux TX static audit**

Read the pinned ESWIN Linux `stmmac_main.c`, `dwmac4_descs.c`,
`dwmac4_dma.c`, and `dwmac4_lib.c` completely around TX descriptor publish,
tail update, completion reclaim, barriers, and IRQ/NAPI completion. Record exact
function names and whether Asterinas agrees. Do not edit production until this
root-cause phase is written in the evidence document.

- [ ] **Step 2: Write pure counter/cadence RED tests**

Freeze component-local snapshots:

```rust
pub(crate) struct QueueProgress {
    pub tx_submitted: u64,
    pub tx_reclaimed: u64,
    pub tx_outstanding: usize,
    pub rx_head: usize,
    pub rx_tail: usize,
}
```

Tests require submitted-reclaimed equals outstanding across wrap/reclaim and
require diagnostic cadence only at cumulative RX powers of two. A source
integration test requires the device line marker and every field.

- [ ] **Step 3: Run RED**

Run the exact production poll/queue tests through
`make test_riscv_dwmac_rx_model`. Expected: missing snapshot/cadence APIs and
source marker assertions fail.

- [ ] **Step 4: Add observational counters only**

Use saturating `u64` counters in `DmaQueue`; update only after successful
descriptor publication/reclaim. Expose a by-value snapshot. Make the existing
`RxPollStats` snapshot available inside the component and add a pure
`should_report_progress(previous, current)` helper. At poll end, after status
service and action selection, emit:

```text
ASTERINAS_GMAC_DATAPATH rx=... rx_budget=... rx_reschedules=... plic_rearms=... tx_submitted=... tx_reclaimed=... tx_outstanding=... rx_head=... rx_tail=... dma_status=0x........
```

The log cadence is powers of two only. No branch controlling descriptors,
MMIO, IRQ masking, or rescheduling may depend on the new counters.

- [ ] **Step 5: Run GREEN and commit**

Run model/production host tests, strict rustfmt, and the pinned RISC-V
`cargo osdk check --ktests -p aster-dwmac -p aster-network -p aster-kernel`.
Commit code/tests and the completed audit together:

```bash
git commit -m "test(net): expose bounded DWMAC datapath progress"
```

### Task 5: Execute the preboard safety gate

**Files:**
- No source changes expected
- Create ignored evidence below `target/megrez-debug/dwmac-high-info/`

- [ ] **Step 1: Run the complete cheap gate once**

Run the focused Megrez debug, network probe, DWMAC model, formatting, compile,
and diff checks. Expected: all pass; git status contains only the two
pre-existing MMC files.

- [ ] **Step 2: Run generic QEMU and recovery evidence**

Build the exact Sv39/SMP4 kernel and probe initramfs in the pinned container.
Run the existing QEMU generic TCP test with the same initramfs and
`asterinas.reboot_after=60`; require the probe terminal marker, automatic
restart boundary, no panic/oops, and no leftover QEMU/container process.

- [ ] **Step 3: Freeze artifact identity**

Create a new debug plan with exact SHA-256/CRC32/size identities, four ordered
payloads, `reboot_after=60`, USB-disabled Megrez DTB, and terminal markers.
Run `megrez_debug check`, simulation, and preboard validation. Verify the board
is still at a responsive U-Boot prompt. Do not invoke `booti` in this task.

### Task 6: Run and classify one physical experiment

**Files:**
- No source changes expected
- Create ignored evidence below the frozen plan output directory

- [ ] **Step 1: Preflight without mutation**

Confirm `/dev/ttyUSB0` is unowned, acquire exclusive access, read one U-Boot
prompt, verify host `enp12s0=10.100.19.216/21`, verify port 18080 is free, and
revalidate all artifact identities. Abort before loading on any mismatch.

- [ ] **Step 2: Execute exactly one boot**

Start the ordered probe/TCP_INFO recorder, reuse matching RAM artifacts or
transfer each mismatched artifact once, run only volatile U-Boot preparation,
then issue one `booti`. Never issue reset, saveenv, or a second booti. Bound the
attempt to 120 seconds and the recovery wait to 90 seconds.

- [ ] **Step 3: Publish and interpret evidence**

Require serial, transport, result, and TCP_INFO JSON bound to the plan. Compare
host bytes-acked/unacked/retransmits with DWMAC RX/TX counters and guest
`current_bytes`, using the classification table in the design. Report either
one selected failing boundary or the exact unresolved pair. Do not make a
production fix or launch another board run in this task.

## Plan self-review

- Every design requirement maps to one task; no task requires touching the
  pre-existing MMC edits.
- Physical mutation occurs only in Task 6 after the complete Task 5 gate.
- TCP capture needs no root privilege and is bounded in time and memory.
- PASS and FAIL both require a new post-terminal U-Boot prompt; stale prompts
  are rejected.
- The plan does not claim hardware-watchdog coverage or universal 99% recovery.
- The only production-driver changes are observational counters proven not to
  control the datapath.
