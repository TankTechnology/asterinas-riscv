# Megrez Debug Workflow M3/M4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the immutable Megrez debug plan into one real Sv39/SMP=4 QEMU fast gate and one cache-aware, single-`booti`, automatically recovering physical-board command.

**Architecture:** Keep `megrez_debug.py` as a thin user entry. A simulation adapter invokes the existing guarded U-Boot preparation/runner and translates its evidence into the shared `StageResult`. A board adapter owns the serial/XMODEM state machine while reusing `BoardSession` parsing and the existing XMODEM sender. Both adapters consume the same frozen `DebugPlan`; neither reconstructs artifact identity or QEMU arguments independently.

**Tech Stack:** Python 3 standard library, `dataclasses`, `subprocess`, `pty`, `termios`, `unittest`, existing guarded QEMU/U-Boot scripts, existing Megrez XMODEM and BoardSession tools, GNU Make.

**Hard constraints:** Asterinas is the only guest under test. The fast gate is generic Sv39 with exactly four harts. A physical run gets one shared 300-second monotonic deadline, performs at most one `booti`, never resets the board, never runs `saveenv`, never transfers the 1-GiB Debian rootfs over XMODEM, and never treats manual reset as ordinary control flow.

---

### Task 1: Register the exact generic Sv39/SMP=4 TCP-probe profile

**Files:**
- Modify: `tools/riscv/qemu_uboot_profiles.py`
- Modify: `tools/riscv/tests/test_qemu_uboot_contracts.py`

- [ ] **Step 1: Write the failing profile test**

Require `profile_by_name("generic-sv39-smp4-tcp-probe")` to use `QEMU_VIRT_SMP4`, `UBOOT_BOOTI`, and a `MEGREZ_TCP_PROBE` scenario whose terminal line is `ASTERINAS_GMAC_TCP_PROBE_READY`. Assert four harts, four `riscv,sv39` CPU nodes, the registered generic-Sv39 CPU override, and no slow-run permit.

```python
profile = profile_by_name("generic-sv39-smp4-tcp-probe")
self.assertIs(profile.machine, QEMU_VIRT_SMP4)
self.assertIs(profile.boot_flow, UBOOT_BOOTI)
self.assertIs(profile.validation, MEGREZ_TCP_PROBE)
self.assertEqual(profile.machine.hart_count, 4)
self.assertEqual(profile.machine.mmu_types, ("riscv,sv39",) * 4)
```

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_qemu_uboot_contracts.ContractCompositionTests -v
```

Expected: the requested profile is not registered.

- [ ] **Step 3: Add only the missing profile composition**

```python
GENERIC_SV39_SMP4_TCP_PROBE = QemuUbootProfile(
    name="generic-sv39-smp4-tcp-probe",
    machine=QEMU_VIRT_SMP4,
    boot_flow=UBOOT_BOOTI,
    validation=MEGREZ_TCP_PROBE,
)
```

Register it without changing `generic-sv39`, LTP, DRM, or legacy Sv48 profiles.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_qemu_uboot_contracts.ContractCompositionTests -v
python3 -m py_compile tools/riscv/qemu_uboot_profiles.py \
  tools/riscv/tests/test_qemu_uboot_contracts.py
ruff check tools/riscv/qemu_uboot_profiles.py tools/riscv/tests/test_qemu_uboot_contracts.py
ruff format --check tools/riscv/qemu_uboot_profiles.py tools/riscv/tests/test_qemu_uboot_contracts.py
git diff --check
git add tools/riscv/qemu_uboot_profiles.py tools/riscv/tests/test_qemu_uboot_contracts.py
git commit -m "test(riscv): register Sv39 SMP4 smoke profile"
```

### Task 2: Bind the guarded QEMU runner to `simulate --tier fast`

**Files:**
- Create: `tools/riscv/megrez_debug_simulation.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing adapter and CLI tests**

Use an injected subprocess runner and freeze this sequence: revalidate the plan; invalidate stale stage result; run `prepare_qemu_uboot_booti.sh prepare` with the plan kernel/initramfs and `generic-sv39-smp4-tcp-probe`; require the generated payload DTB to equal `qemu_dtb`; run `qemu_uboot_booti.py run`; require its guarded result to pass; atomically publish `StageResult(stage="fast", plan_sha256=...)` with relative evidence paths.

Cover prepare/QEMU failure, DTB drift, false or malformed runner results, signal interruption, stale success, unsafe output, arbitrary cwd, and successful evidence publication. No failure may leave `passed:true`.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugSimulationTests -v
```

Expected: the adapter and `simulate` subcommand are missing.

- [ ] **Step 3: Implement the thin adapter**

Add:

```text
megrez_debug.py simulate PLAN --tier fast \
  --output-directory DIR --uboot-build-directory DIR
```

Use explicit argv/environment, `sys.executable`, `PYTHONPATH=tools/riscv`, a bounded timeout, and first-signal propagation. Reuse preparation and runner CLIs; do not copy QEMU argv construction. Add `test_riscv_megrez_debug_fast` as a variable-checking Make alias.

- [ ] **Step 4: Run GREEN and commit**

```bash
make test_riscv_megrez_debug_unit
python3 -m py_compile tools/riscv/megrez_debug.py \
  tools/riscv/megrez_debug_simulation.py tools/riscv/tests/test_megrez_debug.py
ruff check tools/riscv/megrez_debug.py tools/riscv/megrez_debug_simulation.py \
  tools/riscv/tests/test_megrez_debug.py
ruff format --check tools/riscv/megrez_debug.py tools/riscv/megrez_debug_simulation.py \
  tools/riscv/tests/test_megrez_debug.py
git diff --check
git add Makefile tools/riscv/megrez_debug.py tools/riscv/megrez_debug_simulation.py \
  tools/riscv/tests/test_megrez_debug.py
git commit -m "feat(riscv): run Megrez fast simulation gate"
```

### Task 3: Make XMODEM cache-aware and restore 115200 explicitly

**Files:**
- Create: `tools/riscv/megrez_debug_board.py`
- Modify: `tools/riscv/megrez_xmodem.py`
- Modify: `tools/riscv/megrez_board_session.py` only for a genuinely shared parser/fd operation
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_xmodem.py`

- [ ] **Step 1: Write failing PTY/transport tests**

Add a descriptor-based transfer API so one owner holds the serial fd across prompt probe, CRC, transfers, baud restoration, and boot. Prove with PTY/termios evidence:

- prompt detection at 115200 and 1.5 Mbps;
- exact `crc32 ADDRESS SIZE` cache hit/mismatch;
- three hits produce zero XMODEM blocks;
- first miss uses `loadx ADDRESS 1500000`; later misses use `loadx ADDRESS`;
- every transfer rechecks exact size/CRC;
- transient `setenv baudrate 115200` restores U-Boot and host termios before boot;
- no `saveenv`/reset, bounded transcript, short I/O/CRC/signal failure.

Do not accept a mock call list as baud proof; inspect the real PTY slave termios speeds.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_xmodem \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardTransportTests -v
```

Expected: fd/batch transfer and explicit restore APIs are missing.

- [ ] **Step 3: Implement the narrow transport**

Keep `transfer(device, ...)` as a wrapper. Add an fd primitive and exact cache probe. Restore baud non-persistently with `setenv baudrate 115200`, U-Boot's switch handshake, host termios update, Enter, and a fresh prompt. If restoration fails, classify `transport-baud-restore` and forbid `booti`.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_xmodem \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardTransportTests -v
python3 -m py_compile tools/riscv/megrez_debug_board.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
ruff check tools/riscv/megrez_debug_board.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
ruff format --check tools/riscv/megrez_debug_board.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
git diff --check
git add tools/riscv/megrez_debug_board.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
git commit -m "feat(riscv): cache Megrez RAM artifacts"
```

### Task 4: Implement one-deadline, one-`booti` orchestration

**Files:**
- Modify: `tools/riscv/megrez_debug_board.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Write state-machine RED tests**

Define injected operations and a fake monotonic clock. Cover: all cache hits; mixed misses; one absolute deadline passed to every operation; ordered split markers; stable transport/U-Boot/kernel/guest/recovery reasons; failure after the `booti` write without retry; first signal deferred through close/publication; second signal hard exit; recovery timeout publishing false and returning without requesting a reset.

```python
result = run_board(plan, config, operations, started_at=clock())
self.assertEqual(operations.booti_count, 1)
self.assertEqual(result.reason, "board-pass")
```

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardStateTests -v
```

- [ ] **Step 3: Implement the state machine**

Use one `deadline = time.monotonic() + 300.0`; every blocking step receives only the remaining budget. Reset marker classification immediately before the sole `booti` write. Use exact plan addresses/bootargs, then drain through the guest marker until a fresh U-Boot prompt proves `asterinas.reboot_after` recovery. Always close serial and atomically publish log/result bound to the plan hash.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardStateTests -v
make test_riscv_megrez_debug_unit
python3 -m py_compile tools/riscv/megrez_debug_board.py tools/riscv/tests/test_megrez_debug.py
ruff check tools/riscv/megrez_debug_board.py tools/riscv/tests/test_megrez_debug.py
ruff format --check tools/riscv/megrez_debug_board.py tools/riscv/tests/test_megrez_debug.py
git diff --check
git add tools/riscv/megrez_debug_board.py tools/riscv/tests/test_megrez_debug.py
git commit -m "feat(riscv): orchestrate one Megrez boot attempt"
```

### Task 5: Wire real `board` execution and documentation

**Files:**
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/megrez_debug_board.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `Makefile`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Write failing CLI tests**

Before serial open require current artifact identities, a passed matching fast result, safe output, an exclusively held serial device, and `0 < timeout <= 300`. Keep `board --dry-run` byte-compatible and side-effect free. Cover busy serial, result mismatch, artifact drift, arbitrary cwd, stale result invalidation, and signal exit codes.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardCliTests -v
```

Expected: non-dry execution still returns `plan-board-not-implemented`.

- [ ] **Step 3: Wire production operations**

```text
megrez_debug.py board PLAN DEVICE --simulation-result FAST_RESULT \
  --output-directory DIR [--timeout 300]
```

Add a Make alias and document cache, one-boot, recovery, and timeout behavior. Do not invoke Linux, `verify_megrez_sim.sh`, the browser GMAC gate, or a reset mechanism.

- [ ] **Step 4: Run GREEN and commit**

```bash
make test_riscv_megrez_debug_unit
make test_riscv_megrez_gmac_unit
python3 -m py_compile tools/riscv/megrez_debug.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py
ruff check tools/riscv/megrez_debug.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py
ruff format --check tools/riscv/megrez_debug.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py
git diff --check
git add Makefile tools/riscv/megrez_debug.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py tools/riscv/README.md
git commit -m "feat(riscv): run one-command Megrez debug gate"
```

### Task 6: Prove QEMU before one controlled board attempt

**Files:**
- Modify only if evidence exposes a defect in Tasks 1-5
- Keep evidence under ignored `target/megrez-debug/`

- [ ] **Step 1: Run bounded host gates once**

```bash
make test_riscv_megrez_debug_unit
make test_riscv_megrez_gmac_unit
make test_riscv_uboot_booti_unit
git diff --check
```

- [ ] **Step 2: Run the actual fast gate once**

```bash
PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_debug simulate \
  target/megrez-debug/plan.json --tier fast \
  --output-directory target/qemu-uboot/megrez-debug/fast \
  --uboot-build-directory target/qemu-uboot/megrez-debug/uboot
```

Require real argv/log evidence for Sv39 and four harts, ordered Asterinas/guest markers, clean QEMU exit, and a passing result with the exact plan hash.

- [ ] **Step 3: Run one board attempt only when U-Boot is available**

```bash
PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_debug board \
  target/megrez-debug/plan.json /dev/ttyUSB0 \
  --simulation-result target/qemu-uboot/megrez-debug/fast/result.json \
  --output-directory target/megrez-debug/board --timeout 300
```

Accept only exact cache evidence, no more than one `booti`, ordered current-attempt markers, terminal guest evidence, and automatic U-Boot recovery. On serial/recovery failure, publish evidence, release the device, and continue QEMU work without requesting repeated manual resets.

- [ ] **Step 4: Fix only evidence-backed defects**

Reproduce each real failure first in a host/PTY test, implement the smallest correction, rerun the affected unit target and fast QEMU, then consider another physical attempt.

- [ ] **Step 5: Final verification**

```bash
make test_riscv_megrez_debug_unit
make test_riscv_megrez_gmac_unit
make test_riscv_uboot_booti_unit
python3 -m py_compile tools/riscv/megrez_debug*.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py
ruff check tools/riscv/megrez_debug*.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
ruff format --check tools/riscv/megrez_debug*.py tools/riscv/megrez_xmodem.py \
  tools/riscv/megrez_board_session.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_xmodem.py
git diff --check
git status --short
```

### Task 7: Start a separate M6 browser/compatibility slice

- [ ] Preserve the current M6 baseline: DNS, HTTPS 200, Baidu logo asset, NetSurf local-JavaScript `limited-pass`, screenshots, and exact rootfs/kernel identities.
- [ ] Write a separate plan to load `https://www.baidu.com/`, requiring logo, search box, basic text, URL/title, process state, DNS/HTTPS evidence, and screenshot. Do not claim modern JavaScript/login/hot-search compatibility for NetSurf 3.11.
- [ ] Capture the exact `systemd-sysusers` exit/errno and add a separate Asterinas regression for `/proc/sys/fs/nr_open` writes before modifying kernel behavior. Do not hide either issue in guest scripts or move it to the board.
