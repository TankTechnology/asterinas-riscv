# Current-main Megrez Physical Graphics Interaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove three real keyboard-and-pointer interaction cycles on a cold-booted Milk-V Megrez running the current fork main, with correlated evdev, Firefox DOM, screenshot, HDMI, serial, recovery, and artifact-hash evidence.

**Architecture:** Transplant the existing opt-in debug root console onto current `origin/main`, then add a guest-side physical-interaction witness that opens evdev and observes Firefox through Marionette without synthesizing input. A host-side bounded Megrez gate boots the exact artifacts, runs three random-nonce cycles, waits for an external HDMI capture, rejects fatal or stale evidence, and publishes a canonical result only after automatic U-Boot recovery.

**Tech Stack:** Python 3 standard library, Bash, static HTML/JavaScript, Firefox ESR Marionette, Linux evdev UAPI, Asterinas serial debug-console protocol, QEMU HMP, `unittest`, Milk-V Megrez U-Boot.

---

## File map

- `tools/riscv/debian/rootfs/physical_graphics_interaction.html`: deterministic Firefox UI and trusted-event state.
- `tools/riscv/debian/rootfs/physical_graphics_gate.py`: guest evdev and read-only Marionette witness.
- `tools/riscv/tests/test_physical_graphics_gate.py`: page contract, evdev parser, snapshot validator, timeout, and CLI tests.
- `tools/riscv/debian/rootfs/build_rootfs.sh`: install the page and guest gate in `browser-web`.
- `tools/riscv/tests/test_debian_browser_web.py`: freeze browser-web rootfs installation and package identity.
- `tools/riscv/megrez_physical_graphics.py`: host boot/orchestration, marker classifier, HDMI ingestion, recovery, and result publication.
- `tools/riscv/tests/test_megrez_physical_graphics.py`: host contract and lifecycle tests.
- `tools/riscv/physical_graphics_qemu_gate.py`: QEMU adapter that injects keyboard and pointer actions through HMP.
- `tools/riscv/tests/test_physical_graphics_qemu_gate.py`: QEMU argv, HMP sequence, cycle, failure, and cleanup tests.
- `Makefile`: focused unit, QEMU, and physical preparation targets.
- `tools/riscv/README.md`: operator commands and evidence semantics.
- `docs/porting/evidence/2026-09-08-current-main-megrez-physical-graphics.md`: final source, artifact, QEMU, and physical evidence.

### Task 1: Transplant the complete debug-console line

**Files:**

- Modify: the files changed by the nine source commits from `cc1b222f4` through `d77b05f58`
- Verify: `docs/superpowers/specs/2026-09-07-asterinas-debug-root-console-design.md`
- Verify: `docs/superpowers/plans/2026-09-07-asterinas-debug-root-console.md`

- [ ] **Step 1: Record the exact source range**

Run:

```bash
git merge-base d77b05f58 origin/main
git log --reverse --format='%H %s' 374a42092145f143c4dcd0613c909e0d4b2d2800..d77b05f58
```

Expected: merge base `374a42092145f143c4dcd0613c909e0d4b2d2800` and exactly nine commits ending at `d77b05f58`.

- [ ] **Step 2: Cherry-pick the nine commits in order**

Run:

```bash
git cherry-pick \
  cc1b222f42ab6cf02d042fa45b3ee26d2f5006d6 \
  f2c75c010712a59b235aaaf9cbef31d0b544755a \
  c20bfac0f254defff82f72ab4c6eef2fb1af9fcd \
  a20e06d709da2a5b3a0c11f7254e6b6ca603d2c4 \
  7eb204cc35bd92831ae0f6bdde414d429ae1286d \
  9477b4b523369c39491d4e7299fedd376e1b453f \
  3209eddb4aa0f71935aeb6878a113c84dab86fdc \
  0cd2347816cccd48da15bf7bcf64eb25fd57bb2f \
  d77b05f58470877a8fca78594e3e936352d4ddea
```

Resolve conflicts by retaining current-main kernel/rootfs behavior and applying only the debug-console deltas. Do not copy unrelated historical versions of files wholesale.

- [ ] **Step 3: Verify transplant identity and formatting**

Run:

```bash
git log --reverse --format='%H %s' origin/main..HEAD
git diff --check origin/main..HEAD
```

Expected: the design commit followed by nine transplanted commits, with no whitespace errors.

- [ ] **Step 4: Run the transplanted unit tests**

Run:

```bash
make test_riscv_debian_debug_console
PYTHONPATH="$PWD/tools/riscv:$PWD" python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_browser_web \
  tools.riscv.tests.test_megrez_board_session -v
```

Expected: all tests pass with zero failures and zero errors.

### Task 2: Freeze the physical interaction page contract

**Files:**

- Create: `tools/riscv/debian/rootfs/physical_graphics_interaction.html`
- Create: `tools/riscv/tests/test_physical_graphics_gate.py`

- [ ] **Step 1: Write failing page-contract tests**

Add tests that load the page as text and require the exact IDs
`interaction-nonce`, `interaction-button`, `interaction-state`, and
`interaction-instructions`; require listeners for `keydown`, `input`,
`pointermove`, and `click`; require every accepted event to check
`event.isTrusted`; require the button to disable after its first accepted
click; and require a global `window.__asterinasPhysicalGraphicsSnapshot`
function.

The central assertion is:

```python
self.assertIn("window.__asterinasPhysicalGraphicsSnapshot", page)
for event_name in ("keydown", "input", "pointermove", "click"):
    self.assertIn(f'addEventListener("{event_name}"', page)
self.assertGreaterEqual(page.count("event.isTrusted"), 4)
```

- [ ] **Step 2: Run RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_gate.PhysicalGraphicsPageTests -v
```

Expected: FAIL because `physical_graphics_interaction.html` does not exist.

- [ ] **Step 3: Implement the deterministic page**

Create a self-contained page with no external resources. Parse only
`cycle=<1..3>` and `nonce_length=16` from the query string. Focus the input on
load. Keep this state:

```javascript
const state = {
  cycle,
  nonce: "",
  trustedKey: false,
  trustedInput: false,
  trustedPointer: false,
  trustedClick: false,
  clickCount: 0,
  color: "amber"
};
```

On a trusted pointer move, set `trustedPointer`. On trusted keyboard/input
events, update the visible lowercase-hex nonce. Accept the first trusted click
only when the nonce has exactly 16 lowercase hexadecimal characters and a
trusted pointer move was already seen. Set `clickCount=1`, `color="cyan"`,
disable the button, and return a frozen copy of the state from
`window.__asterinasPhysicalGraphicsSnapshot()`.

- [ ] **Step 4: Run GREEN and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_gate.PhysicalGraphicsPageTests -v
git diff --check
git add tools/riscv/debian/rootfs/physical_graphics_interaction.html \
  tools/riscv/tests/test_physical_graphics_gate.py
git commit -m "feat(riscv): add deterministic physical graphics page"
```

Expected: all page-contract tests pass.

### Task 3: Add the guest evdev and read-only Firefox witness

**Files:**

- Create: `tools/riscv/debian/rootfs/physical_graphics_gate.py`
- Modify: `tools/riscv/tests/test_physical_graphics_gate.py`

- [ ] **Step 1: Write failing evdev parser tests**

Use `struct.Struct("=qqHHi")` fixtures and require `EvdevCycle` to:

```python
cycle.feed(event(EV_KEY, 30, 1))
cycle.feed(event(EV_REL, REL_X, 4))
cycle.feed(event(EV_KEY, BTN_LEFT, 1))
cycle.feed(event(EV_KEY, BTN_LEFT, 0))
self.assertEqual(cycle.key_downs, 1)
self.assertEqual(cycle.relative_events, 1)
self.assertTrue(cycle.left_click_complete)
```

Also reject button-up-before-down, counter overflow, truncated records, and an
empty event-node set.

- [ ] **Step 2: Run parser RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_gate.EvdevCycleTests -v
```

Expected: FAIL because `physical_graphics_gate.py` does not exist.

- [ ] **Step 3: Implement the bounded evdev observer**

Implement constants `EV_KEY=1`, `EV_REL=2`, `REL_X=0`, `REL_Y=1`, and
`BTN_LEFT=0x110`. Open `/dev/input/event*` with
`O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW`, require regular character-device
descriptors, drain pending records before READY, and use `selectors` until the
cycle deadline. Count non-button `value==1` key-downs, non-zero X/Y relative
events, and one ordered left-button down/up pair. Cap every counter at 4096.

- [ ] **Step 4: Write failing snapshot and no-synthesis tests**

Require `validate_snapshot(snapshot, expected_nonce, cycle)` to reject every
field drift and accept only:

```python
{
    "cycle": cycle,
    "nonce": expected_nonce,
    "trustedKey": True,
    "trustedInput": True,
    "trustedPointer": True,
    "trustedClick": True,
    "clickCount": 1,
    "color": "cyan",
}
```

Patch the Marionette client and assert that after the READY callback its only
commands are `WebDriver:ExecuteScript` with the literal snapshot expression
and `WebDriver:TakeScreenshot`. Any `PerformActions`, `ElementClick`,
`SendKeys`, or script that mutates the input must raise `GateError`.

- [ ] **Step 5: Run snapshot RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_gate.PhysicalGraphicsSnapshotTests -v
```

Expected: FAIL because the snapshot witness is absent.

- [ ] **Step 6: Implement the Marionette cycle**

Reuse `GateError`, `Marionette`, and `_connect` from
`browser_m5_marionette_gate.py`. Before READY, navigate to:

```text
file:///usr/share/asterinas/physical-graphics/index.html?cycle=N&nonce_length=16
```

and focus `#interaction-nonce`. After READY, poll only
`return window.__asterinasPhysicalGraphicsSnapshot();`. When both evdev and
DOM witnesses pass, capture the viewport PNG, validate its PNG signature and
maximum 16 MiB size, and write it atomically to the evidence directory.

Emit exactly:

```text
ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=N nonce_sha256=<64hex>
ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle=N key_downs=<n> relative_events=<n> left_down=1 left_up=1 digest=<64hex>
ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle=N nonce_sha256=<64hex> trusted_key=1 trusted_input=1 trusted_pointer=1 trusted_click=1 click_count=1 color=cyan
ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle=N sha256=<64hex>
ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=N
```

On failure, emit one `ASTERINAS_PHYSICAL_GRAPHICS_FAIL cycle=N reason=<token>`
record and exit nonzero.

- [ ] **Step 7: Run GREEN and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_gate -v
python3 -m py_compile \
  tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/tests/test_physical_graphics_gate.py
ruff check tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/tests/test_physical_graphics_gate.py
ruff format --check tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/tests/test_physical_graphics_gate.py
git diff --check
git add tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/tests/test_physical_graphics_gate.py
git commit -m "feat(riscv): witness physical graphics interaction"
```

Expected: all tests and static checks pass.

### Task 4: Install the guest witness in the browser-web root

**Files:**

- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write the failing rootfs contract test**

Require the `browser-web` branch of the builder to install:

```text
/usr/share/asterinas/physical-graphics/index.html 0644
/usr/lib/asterinas/physical-graphics-gate 0755
/home/asterinas/physical-graphics-evidence/ 0700 owner 1000:1000
```

Require both source inputs in the builder's fail-closed runtime-input list.

- [ ] **Step 2: Run RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_physical_graphics_witness_is_installed_fail_closed -v
```

Expected: FAIL because the builder does not install the new inputs.

- [ ] **Step 3: Implement the installation**

Install the page and executable only for `browser-web`; create and chown the
evidence directory; keep the existing package tuple unchanged because
`browser-web` already contains Firefox ESR and Python 3.

- [ ] **Step 4: Run GREEN and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_browser_web -v
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
git diff --check
git add tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "feat(riscv): package physical graphics witness"
```

Expected: the browser-web suite passes.

### Task 5: Add the fail-closed host physical gate

**Files:**

- Create: `tools/riscv/megrez_physical_graphics.py`
- Create: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [ ] **Step 1: Write failing marker-classifier tests**

Create fixtures for three cycles with distinct nonce hashes. Require exactly
READY, INPUT, DOM, SCREENSHOT, PASS per cycle, in order. Reject missing,
duplicate, reordered, wrong-cycle, hash-mismatched, weak-count, non-cyan,
post-PASS failure, and transcript-overflow cases. Require the final marker:

```text
ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3
```

- [ ] **Step 2: Run classifier RED**

Run:

```bash
PYTHONPATH="$PWD/tools/riscv:$PWD" python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalMarkerTests -v
```

Expected: FAIL because the host gate does not exist.

- [ ] **Step 3: Implement marker and artifact validation**

Use immutable dataclasses for each cycle and the final result. Bind nonce
plaintext to its SHA-256, require `key_downs>=16`, `relative_events>=1`, one
ordered click, exact screenshot digests, and exactly three cycles. Reuse the
existing debug plan artifact validation for kernel, DTB, Stage1, root image,
manifest, lock, and checksums.

- [ ] **Step 4: Write failing HDMI-ingestion tests**

Test PNG and JPEG signatures, the 1-byte and 64-MiB boundaries, symlink and
non-regular rejection, changing-size rejection, output-alias rejection, and
atomic copy plus SHA-256 publication.

- [ ] **Step 5: Run HDMI RED**

Run:

```bash
PYTHONPATH="$PWD/tools/riscv:$PWD" python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.HdmiEvidenceTests -v
```

Expected: FAIL because HDMI ingestion is absent.

- [ ] **Step 6: Implement the bounded physical lifecycle**

Reuse the board-session boot transaction and debug-console command protocol.
Boot with the existing firmware framebuffer, dual USB host selector,
`asterinas.debug_root_console=1`, and `asterinas.reboot_after=900`. Wait for
root-console READY and verify graphical target, browser service, Xorg fbdev,
Openbox, Firefox, and both input nodes. For cycles 1..3 generate
`secrets.token_hex(8)`, run the guest gate with a 180-second deadline, print
the nonce to the operator, and stream-classify the records. After cycle 3,
wait up to 180 seconds for the explicit `--hdmi-capture` path, ingest it, emit
COMPLETE, drain the transcript, reject fatal markers, and wait for a fresh
U-Boot prompt.

Atomically publish mode-0600 `result.json`, `physical.serial.log`, the three
guest PNGs, `hdmi-evidence.<png|jpg>`, and `sha256sums.txt`. Never publish
`passed: true` before recovery has been observed.

- [ ] **Step 7: Run lifecycle GREEN and commit**

Run:

```bash
PYTHONPATH="$PWD/tools/riscv:$PWD" python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics -v
python3 -m py_compile tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
ruff check tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
ruff format --check tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
git diff --check
git add tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
git commit -m "test(riscv): gate Megrez physical graphics interaction"
```

Expected: all lifecycle tests pass.

### Task 6: Add the QEMU interaction adapter

**Files:**

- Create: `tools/riscv/physical_graphics_qemu_gate.py`
- Create: `tools/riscv/tests/test_physical_graphics_qemu_gate.py`

- [ ] **Step 1: Write failing QEMU contract tests**

Require the argv to use four harts, the existing Sv39 CPU string,
`bochs-display`, `virtio-keyboard-device`, `virtio-tablet-device`, two
virtio-blk devices, one slirp NIC, and HMP on a private Unix socket. For each
cycle require HMP to inject 16 hexadecimal keys, relative pointer movement,
left-button down, and left-button up only after the guest READY record. Reject
any xdotool, Marionette input-dispatch, VNC, or GTK-display fallback.

- [ ] **Step 2: Run RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_qemu_gate -v
```

Expected: FAIL because the adapter does not exist.

- [ ] **Step 3: Implement the adapter**

Reuse the debug-root-console QEMU session, browser-web artifact validation,
and physical marker classifier. Convert nonce characters to QEMU `sendkey`
names, use HMP relative-pointer and button commands, capture a PPM after each
PASS, and require the existing rendered-pixel thresholds. Publish a separate
QEMU result whose `physical` field is always `false`.

- [ ] **Step 4: Run GREEN and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_physical_graphics_qemu_gate -v
python3 -m py_compile tools/riscv/physical_graphics_qemu_gate.py \
  tools/riscv/tests/test_physical_graphics_qemu_gate.py
ruff check tools/riscv/physical_graphics_qemu_gate.py \
  tools/riscv/tests/test_physical_graphics_qemu_gate.py
ruff format --check tools/riscv/physical_graphics_qemu_gate.py \
  tools/riscv/tests/test_physical_graphics_qemu_gate.py
git diff --check
git add tools/riscv/physical_graphics_qemu_gate.py \
  tools/riscv/tests/test_physical_graphics_qemu_gate.py
git commit -m "test(riscv): automate physical graphics contract in QEMU"
```

Expected: all QEMU adapter tests pass.

### Task 7: Wire targets and document exact operation

**Files:**

- Modify: `Makefile`
- Modify: `tools/riscv/README.md`
- Modify: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [ ] **Step 1: Write failing documentation and Make-target tests**

Require targets `test_riscv_physical_graphics_unit`,
`test_riscv_physical_graphics_qemu_gate`, and
`prepare_riscv_megrez_physical_graphics`. Require the operator guide to name
the current-main source identity, seven immutable input arguments,
`--hdmi-capture`, three 180-second interaction windows, the 900-second guest
recovery, and the fact that QEMU cannot satisfy the physical result.

- [ ] **Step 2: Run RED**

Run:

```bash
PYTHONPATH="$PWD/tools/riscv:$PWD" python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.DocumentationTests -v
```

Expected: FAIL because targets and documentation are absent.

- [ ] **Step 3: Add targets and operator commands**

The unit target runs all three new test modules. The QEMU target requires all
browser-web artifacts and an output directory. The prepare target validates
artifacts and prints the exact physical command but cannot open the serial
device or modify U-Boot state.

- [ ] **Step 4: Run GREEN and commit**

Run:

```bash
make test_riscv_physical_graphics_unit
git diff --check
git add Makefile tools/riscv/README.md \
  tools/riscv/tests/test_megrez_physical_graphics.py
git commit -m "docs(riscv): document physical graphics interaction gate"
```

Expected: unit target passes and the documented commands match the parser.

### Task 8: Run current-main QEMU gates

**Files:**

- Runtime only: `target/current-main-physical-graphics/qemu-debug-console/`
- Runtime only: `target/current-main-physical-graphics/qemu-browser-web/`
- Runtime only: `target/current-main-physical-graphics/qemu-interaction/`

- [ ] **Step 1: Build the current-main RISC-V kernel and browser-web root**

Use the pinned Asterinas container from `AGENTS.md`, `TARGET_ARCH=riscv64`,
`SMP=4`, and the existing browser-web package cache. Record kernel and rootfs
hashes before launching QEMU.

- [ ] **Step 2: Run the debug-root-console QEMU gate**

Run the documented `make test_riscv_debian_debug_console_qemu_gate` command
with the current kernel, U-Boot, DTB, Stage1, browser-web root, manifest, lock,
checksums, and a fresh output directory.

Expected: one READY, all UID/PID1/ext2/graphical/desktop probes pass, one
rendered non-blank PPM, and complete cleanup.

- [ ] **Step 3: Run the browser-web QEMU gate**

Run the existing browser-web gate against the same immutable inputs.

Expected: browser-web result passes, all 20 controlled fixture requests are
present, the final 1280x1024 framebuffer is non-blank, and no fatal marker is
present in the drained transcript.

- [ ] **Step 4: Run the three-cycle interaction QEMU gate**

Run `make test_riscv_physical_graphics_qemu_gate` with the same immutable
inputs and a fresh output directory.

Expected: three distinct nonce hashes, three complete INPUT/DOM/SCREENSHOT/PASS
sequences, three non-blank captures, no fatal marker, and complete cleanup.

### Task 9: Run the bounded physical Megrez gate

**Files:**

- Runtime only: `target/current-main-physical-graphics/physical/`
- Create: `docs/porting/evidence/2026-09-08-current-main-megrez-physical-graphics.md`

- [ ] **Step 1: Freeze inputs and prepare a capture path**

Copy the current kernel, frozen Megrez DTB, Stage1 archive, browser-web root,
manifest, lock, and checksums into the run directory with mode 0600. Reserve
`target/current-main-physical-graphics/physical/operator-hdmi.png` for the
operator or capture-card image; do not create it before the final interaction.

- [ ] **Step 2: Launch one cold-boot physical gate**

Run the documented `tools.riscv.megrez_physical_graphics` command with the
stable `/dev/serial/by-id/...` device, exact CRC map, immutable inputs,
`--hdmi-capture` path, and output directory. Do not persist U-Boot environment
changes.

- [ ] **Step 3: Perform the three prompted interactions**

For each cycle, type the printed 16-character nonce into the focused Firefox
field, move the physical pointer, and click the large button once. After cycle
3, capture the HDMI display into the reserved path while the cyan PASS state
is visible.

Expected: the host observes three complete correlated cycles and ingests the
HDMI image.

- [ ] **Step 4: Verify recovery and evidence**

Wait for the 900-second guest timer and a fresh U-Boot prompt. Verify
`result.json` with the gate's `verify` subcommand, recompute every SHA-256,
scan the full transcript for fatal markers, and visually inspect the HDMI and
browser screenshots.

Expected: `passed=true`, `cycles=3`, `physical=true`, `recovery_observed=true`,
and no missing or mismatched artifact.

- [ ] **Step 5: Write and commit the evidence report**

Record branch and commit, hardware topology, exact commands, package/kernel
identity, QEMU results, physical result, screenshot and HDMI dimensions,
every retained SHA-256, acceptance boundaries, and any isolated failure. Then
run:

```bash
git add docs/porting/evidence/2026-09-08-current-main-megrez-physical-graphics.md
git commit -m "docs(riscv): record current-main physical graphics evidence"
```

### Task 10: Final verification and review

**Files:**

- Verify: all files changed from `origin/main..HEAD`

- [ ] **Step 1: Run the complete focused verification**

Run:

```bash
make test_riscv_physical_graphics_unit
make test_riscv_debian_debug_console
PYTHONPATH="$PWD/tools/riscv:$PWD" python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_browser_web \
  tools.riscv.tests.test_megrez_board_session -v
python3 -m py_compile \
  tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/megrez_physical_graphics.py \
  tools/riscv/physical_graphics_qemu_gate.py
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
ruff check tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/megrez_physical_graphics.py \
  tools/riscv/physical_graphics_qemu_gate.py \
  tools/riscv/tests/test_physical_graphics_gate.py \
  tools/riscv/tests/test_megrez_physical_graphics.py \
  tools/riscv/tests/test_physical_graphics_qemu_gate.py
ruff format --check tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/megrez_physical_graphics.py \
  tools/riscv/physical_graphics_qemu_gate.py \
  tools/riscv/tests/test_physical_graphics_gate.py \
  tools/riscv/tests/test_megrez_physical_graphics.py \
  tools/riscv/tests/test_physical_graphics_qemu_gate.py
git diff --check origin/main..HEAD
```

Expected: every command exits zero.

- [ ] **Step 2: Audit the completion evidence**

Confirm the current-main QEMU results and physical `result.json` cover every
acceptance item in the design. Confirm the physical HDMI file and three guest
screenshots open successfully and show the expected cyan state and nonce.

- [ ] **Step 3: Request code review**

Review `origin/main..HEAD` against the Asterinas maintainability, development,
security, hardware, and documentation persona checklists. Fix every blocking
finding and rerun affected verification before branch completion.
