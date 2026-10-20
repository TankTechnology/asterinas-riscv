# fbdev and Single-Packet Physical Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Qualify the current Asterinas desktop fallback and GMAC with two independent QEMU gates, then run exactly one recoverable physical network transaction and one current-candidate fb0 desktop boot without involving DRM, GPU, or network stress.

**Architecture:** Keep the existing fbdev provider as the only display path. Repair its QEMU device/session handoff and validate a deterministic framebuffer pattern before allowing a physical boot. Add a separate UDP echo fixture for one application datagram in each direction; do not reuse the existing 20-request HTTP milestone. Both physical runners must own the serial lock, arm the hardware watchdog, use a fresh firmware epoch, stop on the first timeout, and require RockOS recovery plus a fresh authenticated nonce after the experiment.

**Tech Stack:** Python gate runners and unit tests, POSIX shell evidence helpers, Asterinas fbdev and DWMAC markers, QEMU bochs/simple-framebuffer scanout, U-Boot/OpenSBI, Megrez RockOS serial/SSH recovery.

---

## Acceptance contract

The work is complete only when every item below has a retained `result.json`, bounded serial log, artifact identity, and test command:

| Gate | Required evidence | Forbidden side effects |
| --- | --- | --- |
| QEMU fbdev | `/dev/fb0` metadata, 4 KiB read hash, deterministic multi-color write/read/restore, Xorg/session marker, host scanout check, software reboot to fresh OpenSBI/U-Boot | DRM device, GPU registers, Firefox, network traffic |
| QEMU single packet | one fixed UDP payload received once by the fixture and echoed once, guest TX/RX/error marker, recovery | HTTP loop, retries, stress, browser, display session |
| Physical single packet | fresh U-Boot epoch, current candidate identity, watchdog, exactly one UDP datagram each way, RockOS recovery and root nonce reopen | persistent menu change, second packet attempt, retry after timeout |
| Physical fb0 desktop | fresh U-Boot epoch, watchdog, current candidate identity, fb0 mode/read/write/restore, desktop marker, RockOS recovery and root nonce reopen | DRM/GPU/GMAC/DWC3 probes, Firefox, network pressure |

The absence of a video capture device limits the physical display claim to kernel fb0 and desktop-session evidence. It must not be reported as proof that HDMI pixels were visible on the panel.

## Test contract and execution boundaries

The display qualification is a layered test suite, not an unrestricted
end-to-end script:

| Layer | Runs where | What is asserted | Hardware side effects |
| --- | --- | --- | --- |
| Unit | host Python test process | framebuffer layout/color packing, clipped writes and exact restore; marker grammar; candidate-hash binding; nonce/boot-id state transitions; forbidden boot arguments | none |
| QEMU integration | one immutable QEMU artifact set | `/dev/fb0` metadata and sparse readback, deterministic three-color client paint, X11/fb0/host-pixel agreement, ordered desktop markers, one software-reboot recovery | no board, no DRM/GPU, no network |
| Physical preflight | host plus serial read-only checks | fresh U-Boot epoch, one serial owner, watchdog readback, artifact and install receipts, root control-chain capability | no Asterinas boot until every check passes |
| Physical acceptance | one watchdog-bounded board cycle | exactly one fb0 write/read/restore, one desktop marker, fresh firmware epoch, RockOS recovery, reopened serial, nonce-bound UID 0 and boot-id | one boot, no retry, no network/DRM/GPU |

The unit layer must run on every change. The QEMU integration layer is the
only place where a display implementation is debugged. A physical run is
authorized only by a machine-readable QEMU receipt and a separate partition-2
installation receipt; its result is either a single pass or a preserved
failure receipt.

Before physical execution, the runner must be corrected to use the shared
control-chain handshake (`ASTERINAS_CONTROL`, UID 0, fresh nonce and boot-id)
after closing and reopening the serial device. A RockOS login that only proves
UID 1000 is not sufficient for recovery and must be rejected by the unit tests.

### Fixed display cases

| Case | Test input | Pass condition |
| --- | --- | --- |
| D0 layout | `1280x1024`, `1920x1080`, padded stride; malformed format, short aperture, invalid offsets | Valid layouts produce exact offsets and BGR bytes; invalid layouts cause no write |
| D1 isolated fb0 transaction | Xorg is not running; save one 16x16 region, write a fixed color through shared mmap, read that region through the fd, restore | Exact expected bytes, no change outside the region including row padding, identical saved/restored SHA-256 |
| D2 Xorg paint | Same tiny X11 client on QEMU and board; one override-redirect window, three known colors, explicit XSync | `XGetImage` and fd readback agree at all fixed sample coordinates; bounded request completion |
| D3 QEMU scanout | Capture while the D2 window is held and after its ready marker | Every fixed host PPM coordinate equals the expected RGB; color count alone is insufficient |
| D4 desktop session | Remove the diagnostic window; retain normal panel, desktop and terminal | Correct window geometry and window-tree roles; pixels from normal clients separately recorded, not from the canary |
| D5 recovery | One synced guest reboot or watchdog fallback | Fresh firmware epoch, RockOS UID 0/boot-id, serial close/reopen, second distinct nonce and same recovered boot-id |
| D6 failure paths | Fake serial/QEMU adapters inject timeout, short read, wrong color, restore failure and stale nonce at each boundary | Failed component is named; recovery attempted once where possible; no second experimental boot or MMIO retry |

D1 and D2 must never run competing framebuffer writers. In particular, a
full-screen canary must not overwrite an X11 diagnostic while D2/D3 are being
measured. The retained X11 boundary result is preliminary: its sampled PPM
coordinates have been checked, but its throwaway runner did not yet enforce
the final marker/capture ordering. Production D3 requires that ordering.

Guest helpers are shared across QEMU and the board. Only transport, actual
framebuffer geometry and the scanout observer differ. Every receipt includes
case ID, run nonce, kernel/Stage1/helper identities, expected and actual pixels,
phase latency, deadline and recovery outcome. Timings are measured separately
from correctness, with fixed deadlines in the runner rather than enlarged on
a failed run.

D0/D6 execute on the host without board I/O. D1-D5 use QEMU before their first
physical use. On the board the approved fb0 session performs at most one
bounded transaction and one X11 paint check, followed by recovery. Passing
QEMU never proves board MMIO/cache/HDMI behavior; physical receipts remain
separate. Without external capture, D3's physical HDMI counterpart is marked
unobserved, not passed.

## Files and responsibilities

Existing files to inspect and keep behavior-compatible:

- `tools/riscv/debian/rootfs/desktop_m4_framebuffer_probe.py`: pure framebuffer layout, color packing, bounded read/write/restore, and hash markers.
- `tools/riscv/debian/rootfs/desktop_m4_device_access.sh`: bounded `/dev/fb0` and input-device handoff before Xorg.
- `tools/riscv/debian/rootfs/desktop_m4_evidence.sh`: ordered session, Xorg, framebuffer, and reboot markers.
- `tools/riscv/debian/rootfs/desktop_m4_gate.py`: QEMU fbdev orchestration and host scanout classification.
- `tools/riscv/debian/rootfs/desktop_fbdev_qemu.conf`: explicit QEMU Xorg/fbdev configuration.
- `tools/riscv/megrez_menu_board.py`: existing single-owner serial menu runner and watchdog probe cycle.
- `tools/riscv/megrez_gmac_gate.py`: existing 20-request network gate; it remains unchanged for this goal except for shared parser helpers.
- `tools/riscv/megrez_gmac_evidence.py`: shared GMAC datapath parser and error checks.
- `tools/riscv/megrez_control_chain.py`: nonce, UID, boot-id, and serial-reopen contract.
- `tools/riscv/tests/test_debian_rootfs.py`, `tools/riscv/tests/test_megrez_gmac_gate.py`, and `tools/riscv/tests/test_megrez_gmac_evidence.py`: existing regression suites.

New files:

- `tools/riscv/megrez_single_packet_fixture.py`: bounded UDP echo fixture; accepts one nonce-bound datagram, records one request and one response, and rejects duplicates.
- `tools/riscv/megrez_single_packet_gate.py`: QEMU/physical-independent packet contract and classifier; physical adapter owns watchdog, serial, and recovery.
- `tools/riscv/tests/test_megrez_single_packet_gate.py`: parser, exact-count, nonce, timeout, and recovery tests.
- `tools/riscv/megrez_display_gate.py`: physical fb0 desktop runner with one watchdog-guarded cycle and no network/GPU selection.
- `tools/riscv/megrez_single_packet_physical_gate.py`: physical one-shot UDP runner; it requires a QEMU packet receipt and an explicit rootfs-service installation receipt before loading Asterinas.
- `tools/riscv/tests/test_megrez_display_gate.py`: display runner state-machine and recovery tests.
- `docs/porting/evidence/2026-10-10-fbdev-single-packet-qualification.md`: final evidence index and explicit limitations.

All large QEMU images and scratch logs must stay in `/tmp` or existing target directories; do not create another rootfs copy while `/mnt/shared` is near capacity.

### Task 1: Freeze the baseline and prove the current physical recovery state

**Files:**

- Create: `docs/porting/evidence/2026-10-10-fbdev-single-packet-qualification.md`
- Test: existing result JSON files under `target/megrez-physical-step1/`

- [x] **Step 1: Record the current candidate identities.**

  Read the existing manifest and record its menu SHA, kernel, Stage1, and DTB SHA-256 values. Do not rebuild or publish an artifact.

- [x] **Step 2: Validate the prior independent receipts.**

  Require `passed=true` and `recovered=true` for Basic, DWC3-0, DWC3-1, GMAC-0, GMAC-1, and PHY. Treat the normal registration receipt separately: require `ASTERINAS_GMAC_READY`, `tx_submitted=0`, and zero TX/RX errors, while recording that RX background work was observed.

- [x] **Step 3: Verify RockOS without rebooting the board.**

  Use SSH read-only commands for UID, boot-id, uptime, and link state. Use the existing serial lock only to issue one nonce-framed root command, close, reopen, and issue a second nonce-framed root command. Leave the board in RockOS.

- [x] **Step 4: Run the focused host tests before code changes.**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_megrez_gmac_evidence \
    tools.riscv.tests.test_megrez_gmac_gate \
    tools.riscv.tests.test_megrez_control_chain \
    tools.riscv.tests.test_megrez_hardware_probe \
    tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe
  git diff --check
  ```

  Expected: all focused tests pass; no board state changes.

### Task 2: Repair the QEMU fbdev user-space handoff with tests first

**Files:**

- Modify: `tools/riscv/debian/rootfs/desktop_m4_framebuffer_probe.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_device_access.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_evidence.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_gate.py`
- Modify: `tools/riscv/debian/rootfs/desktop_fbdev_qemu.conf`
- Test: `tools/riscv/tests/test_debian_rootfs.py`
- Test: create focused framebuffer tests in `tools/riscv/tests/test_debian_desktop_m4_framebuffer_probe.py`

- [x] **Step 1: Write failing pure-function tests.**

  Cover `1280x1024x32` and `1920x1080x32`, padded stride, `BgrReserved`, short reads, clipped rectangles, out-of-bounds writes, and a three-color pattern that rejects a single-color frame. The test must assert that the original bytes are restored exactly.

- [x] **Step 2: Run the tests and confirm the expected red state.**

  ```bash
  python3 -m unittest tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe -v
  ```

  The initial failure must be a missing or incomplete contract, not an import or syntax error.

- [x] **Step 3: Implement the minimal bounded framebuffer contract.**

  Keep the current WriteCombining mapping. Read and hash the first 4096 bytes; write three small, clipped rectangles using the reported stride and BGR packing; flush with the existing bounded mechanism; read back and hash; restore the saved bytes; report `FB_IOCTL`, `FB_SAMPLE`, `FB_COLORS`, `FB_RESTORE`, and `READY`. Reject non-32-bpp layouts, dimensions above 8192, short reads, and writes outside the reported buffer.

- [x] **Step 4: Make device handoff deterministic.**

  Ensure `desktop_m4_device_access.sh` creates or validates `/dev/fb0` before Xorg, uses one bounded `timeout`, emits a failure marker on timeout, and never starts duplicate core/evidence services. Keep input optional for the fbdev gate.

- [x] **Step 5: Make Xorg/session ordering explicit.**

  Configure the fbdev provider before Xorg starts, remove any legacy configuration that points to absent input nodes, and require the session marker only after the framebuffer probe and Xorg socket are ready. The software reboot timer must remain enabled and bounded.

- [x] **Step 6: Run the focused tests and shell checks.**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe \
    tools.riscv.tests.test_debian_rootfs
  bash -n tools/riscv/debian/rootfs/desktop_m4_device_access.sh \
    tools/riscv/debian/rootfs/desktop_m4_evidence.sh
  ```

### Task 3: Pass the QEMU fbdev gate and software recovery

**Files:**

- Modify only if required: `tools/riscv/debian/rootfs/desktop_m4_gate.py`
- Evidence: `target/qemu-desktop-m4-fbdev-20261010/` or an equivalent `/tmp` run directory

- [x] **Step 1: Assemble one immutable QEMU input set.**

  Use the current RISC-V kernel, cached U-Boot, simple-framebuffer DTB, Stage1, and one existing desktop-m4 rootfs. Record hashes before starting QEMU. Do not copy a rootfs to `/mnt/shared`.

- [ ] **Step 2: Run one bounded QEMU fbdev cycle.**

  Require the ordered markers for `/dev/fb0`, Xorg/session, the three-color pattern, restore, desktop ready, and software reboot. Capture the QEMU HMP screenshot or scanout digest after the pattern and require more than one non-background color. A guest-only hash is insufficient for the host scanout claim.

- [x] **Step 2a: Isolate the client-to-scanout boundary once.**

  A bounded diagnostic client painted three X11 bands and compared, at the
  same coordinates, `XGetImage`, guest `/dev/fb0`, and an early QEMU capture.
  All three agreed and the host capture was multicolor. This proves that the
  basic X11-to-fbdev-to-scanout path can carry application pixels, but it does
  not turn the pattern-only receipt into the strict normal-desktop receipt.

- [ ] **Step 3: Classify failures by boundary.**

  - Missing `/dev/fb0`: device-node/service handoff failure.
  - Missing Xorg socket/session: Xorg or user-session failure.
  - Guest pattern passes but host scanout is uniform: scanout/cache/flush boundary failure.
  - One bounded X11 client agrees across X11, `/dev/fb0`, and host capture but
    the strict desktop screenshot is uniform: desktop settling/window-state
    capture failure; keep the physical display gate closed.
  - Desktop marker passes but fresh OpenSBI/U-Boot is absent: recovery failure.

  Stop at the first classification; do not add retries or increase the deadline to hide it.

- [ ] **Step 4: Require the QEMU receipt.**

  The gate is green only with `passed=true`, fresh recovery, framebuffer metadata, read/write/restore hashes, and host scanout evidence. This result is a hard prerequisite for physical fb0.

### Task 4: Add an independent exact-one UDP packet gate

**Files:**

- Create: `tools/riscv/megrez_single_packet_fixture.py`
- Create: `tools/riscv/megrez_single_packet_gate.py`
- Test: `tools/riscv/tests/test_megrez_single_packet_gate.py`
- Reuse: `tools/riscv/megrez_gmac_evidence.py`

- [x] **Step 1: Define the packet contract in tests.**

  Use a fixed nonce-bound payload and require exactly one fixture datagram with the expected source, destination, length, and SHA-256, followed by exactly one matching echo. Reject a duplicate, stale nonce, wrong payload, missing response, or a second attempt. GMAC errors and fatal DMA status must remain zero.

- [x] **Step 2: Run the packet tests red.**

  ```bash
  python3 -m unittest tools.riscv.tests.test_megrez_single_packet_gate -v
  ```

  Include tests for zero, one, and two fixture requests; timeout followed by recovery; and a stale nonce. Failures must be contract failures, not parser errors.

- [x] **Step 3: Implement the UDP fixture.**

  Bind one fixed host UDP port, accept only the board address, validate the 32-byte nonce and fixed payload, echo once, close the socket, and write a JSON summary. The fixture must not retry or serve a second datagram.

- [x] **Step 4: Implement the QEMU/host classifier.**

  Separate business datagrams from ARP, DHCP, TCP, and control traffic. Require the fixture count to be exactly one request and one response; use `ASTERINAS_GMAC_DATAPATH` only for zero-error and non-fatal-DMA corroboration. Do not change `FIXTURE_REQUESTS=20` in the existing HTTP gate.

- [x] **Step 5: Verify the gate in QEMU first.**

  Run the DWMAC model/descriptor loopback with one fixed UDP transaction, then the userspace UDP fixture. Retain the packet summary, guest serial log, and recovery result. No display or browser service is enabled.

### Task 5: Run exactly one physical single-packet transaction

**Files:**

- Modify only if required: `tools/riscv/megrez_single_packet_gate.py`
- Create: `tools/riscv/megrez_single_packet_physical_gate.py`
- Evidence: `target/megrez-physical-step1/gmac-single-packet-cycle-<date>/`

- [ ] **Step 1: Perform the preflight without loading Asterinas.**

  Confirm the board is RockOS, serial has one owner, SSH/root nonce is fresh, the current candidate manifest hashes match, and a new U-Boot/OpenSBI epoch is observable. If any check fails, stop without sending a boot command.

- [ ] **Step 2: Arm the hardware watchdog at the fresh U-Boot prompt.**

  Use the existing watchdog helper and verify clock, reset, component type, control, and timeout readback. A failed watchdog prerequisite blocks the experiment.

- [ ] **Step 3: Select one network entry and send one fixed UDP datagram.**

  Start the one-shot fixture, boot the current candidate, and wait for the exact-one marker. Do not retry a serial read, packet, or MMIO operation. Do not run HTTP, ping, DNS, Firefox, or desktop services. The runner must refuse to start unless a separate rootfs installation receipt binds the `asterinas-single-packet` service to the partition-2 image.

- [ ] **Step 4: Wait for recovery regardless of pass/fail.**

  Require a fresh OpenSBI/U-Boot epoch from the watchdog or guest reboot. Then boot RockOS, close/reopen serial, and verify a new root nonce, UID 0, and boot-id. Leave RockOS running.

- [ ] **Step 5: Classify the result.**

  `passed=true` requires exactly one fixture request/response, guest marker, zero errors, and full recovery. A timeout or duplicate packet is a failed receipt, but remains useful evidence; it never triggers a second physical attempt in the same run.

### Task 6: Implement the physical fb0 desktop runner

**Files:**

- Create: `tools/riscv/megrez_display_gate.py`
- Test: `tools/riscv/tests/test_megrez_display_gate.py`
- Reuse: `tools/riscv/megrez_control_chain.py`, `tools/riscv/megrez_menu_board.py`, and the current candidate manifest
- Evidence: `target/megrez-physical-step1/fb0-desktop-cycle-<date>/`

- [ ] **Step 1: Write the state-machine tests.**

  Accept only `RockOS → fresh U-Boot → watchdog armed → Asterinas fb0 desktop → recovery → RockOS → serial reopen`. Reject a missing watchdog, reused nonce, second boot attempt, DRM/GPU/GMAC/DWC3 boot argument, missing restore hash, or missing recovery frame.

- [ ] **Step 2: Implement one bounded display cycle.**

  Select the stable fbdev desktop entry, pass no DRM/GPU/network arguments, wait for the desktop marker, read/hash the first 4096 bytes, write one small fixed region, restore it, and record mode/stride/pixel format. Keep the existing hardware watchdog and guest recovery deadline.

- [ ] **Step 3: Verify recovery and serial ownership.**

  After the desktop marker and fb0 restore, allow exactly one `sync; reboot -f` or watchdog recovery. Reopen the serial device, verify a fresh nonce and UID 0, then verify the same boot-id over SSH and leave RockOS selected. A missing response stops the plan and requires operator recovery before any new board run.

- [x] **Step 4: Run only after Task 3 is green.**

  The runner must refuse to start if the QEMU fbdev receipt is absent, stale, or failed. It must also refuse to start if the current candidate hashes differ from the receipt or if partition 2 lacks a separately recorded `desktop-m4-framebuffer-rw` installation receipt. Pattern-only QEMU qualification requires the explicit `--allow-pattern-only` flag and never asserts application painting.

### Task 7: Final verification and evidence review

**Files:**

- Modify: `docs/porting/evidence/2026-10-10-fbdev-single-packet-qualification.md`
- Test: all new and affected focused suites

- [x] **Step 1: Run the complete focused software suite.**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe \
    tools.riscv.tests.test_megrez_single_packet_gate \
    tools.riscv.tests.test_megrez_display_gate \
    tools.riscv.tests.test_megrez_gmac_evidence \
    tools.riscv.tests.test_megrez_gmac_gate \
    tools.riscv.tests.test_megrez_control_chain \
    tools.riscv.tests.test_megrez_hardware_probe
  git diff --check
  ```

- [ ] **Step 2: Review the physical receipts.**

  Confirm that each physical directory contains one serial log, one result JSON, artifact hashes, watchdog status, nonce/boot-id records, and a recovery classification. Confirm no credential, password, or private network key is present.

- [ ] **Step 3: Review the scope boundary.**

  Reject the result if it contains PowerVR, DRM/KMS, GPU register access, Firefox, HTTP stress, repeated physical attempts, or a persistent U-Boot/menu modification.

- [ ] **Step 4: Record remaining limitations.**

  State separately whether QEMU scanout passed, whether the physical fb0 desktop session passed, and whether panel-visible HDMI pixels were externally observed. Do not collapse these into one “display works” claim.

## Stop conditions

Stop immediately and preserve the receipt if any of these occurs: serial lock cannot be acquired; fresh OpenSBI/U-Boot is absent; watchdog cannot be armed/read back; a command or MMIO read times out; a second packet is observed; a framebuffer restore hash differs; a desktop marker is missing; recovery does not produce a fresh firmware epoch; or the serial device cannot be reopened. No automatic retry is permitted for a physical experiment.

## Execution order

1. Task 1 baseline and recovery confirmation.
2. Task 2 QEMU fbdev repair and focused tests.
3. Task 3 QEMU fbdev gate plus software reboot recovery.
4. Task 4 single-packet fixture, tests, and QEMU gate.
5. Task 5 one physical single-packet cycle.
6. Task 6 one physical fb0 desktop cycle.
7. Task 7 evidence review.

The physical board is never used to debug an unqualified QEMU behavior, and a failed physical cycle never triggers a retry in the same session.
