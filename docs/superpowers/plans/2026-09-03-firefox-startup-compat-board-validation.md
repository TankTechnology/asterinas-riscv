# Firefox Startup Compatibility and Megrez Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove Firefox's startup prerequisites on the already admitted QEMU network image, then perform one bounded Megrez desktop/network/Firefox-window validation without rebuilding the base root filesystem.

**Architecture:** Keep the signed Debian 13.6 `browser-web` ext2 image immutable and apply only the repository's deterministic development overlay when scripts change. Use the existing SMP=4 Sv39 QEMU startup sampler to stop at Marionette readiness and retain bounded diagnostics; only after that gate passes, build a separate default/Sv48 Megrez kernel and run the physical gate with pinned artifacts, a serial deadline, and software-reboot recovery.

**Tech Stack:** Python 3 `unittest`, Bash, Docker, QEMU `virt` SMP=4, U-Boot, Debian RISC-V Firefox ESR 140, Xorg/Openbox, Marionette, Asterinas Sv39/Sv48 kernels, Milk-V Megrez EIC7700X GMAC.

---

## File structure

- `tools/riscv/debian/rootfs/firefox_startup_profile.py`: existing bounded QEMU sampler for basic target, X socket, Firefox exec, and Marionette readiness.
- `tools/riscv/debian/rootfs/browser_web_firefox.sh`: Firefox launch/profile setup; change only if the startup transcript identifies a concrete incompatibility.
- `tools/riscv/debian/rootfs/browser_web_evidence.sh`: bounded guest evidence and failure markers; change only with a failing unit test.
- `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`: QEMU launch, artifact validation, serial cleanup, and retained evidence.
- `tools/riscv/tests/test_debian_browser_web.py`: unit and negative tests for any compatibility or diagnostic fix.
- `tools/riscv/megrez_gmac_gate.py`: existing physical network/Firefox target and result publisher.
- `target/dev-overlays/browser-web/`: ignored, reusable rootfs/overlay and QEMU evidence; never committed.

### Task 1: Freeze the current baseline and artifact identities

- [ ] **Step 1: Run the host-only Firefox contract suite**

Run:

```bash
tools/riscv/firefox_fast_check.sh
```

Expected: `Ran 93 tests`, `OK`, and `FIREFOX_FAST_CHECK_PASS` without starting QEMU.

- [ ] **Step 2: Record immutable input hashes**

Run:

```bash
sha256sum \
  target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  target/qemu-uboot/cache/u-boot-build-browser-web-aa6f7533/u-boot \
  target/qemu-uboot/browser-web-aa6f7533/qemu-virt.dtb \
  target/debian-riscv/stage1/initramfs.cpio \
  target/dev-overlays/browser-web/rootfs/debian-root.ext2 \
  target/dev-overlays/browser-web/rootfs/rootfs-manifest.json
```

Expected: six non-empty SHA-256 records. The QEMU kernel must have been built with `FEATURES=riscv_sv39_mode`.

### Task 2: Prove Firefox startup in QEMU without running the web workload

- [ ] **Step 1: Run one bounded SMP=4 startup sample in the project container**

Run from the worktree mounted at `/root/asterinas`:

```bash
python3 tools/riscv/debian/rootfs/firefox_startup_profile.py \
  --kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  --uboot target/qemu-uboot/cache/u-boot-build-browser-web-aa6f7533/u-boot \
  --dtb target/qemu-uboot/browser-web-aa6f7533/qemu-virt.dtb \
  --stage1-initramfs target/debian-riscv/stage1/initramfs.cpio \
  --root-image target/dev-overlays/browser-web/rootfs/debian-root.ext2 \
  --root-manifest target/dev-overlays/browser-web/rootfs/rootfs-manifest.json \
  --packages-lock target/dev-overlays/browser-web/rootfs/packages.lock \
  --package-checksums target/dev-overlays/browser-web/rootfs/source-metadata/package-checksums \
  --output-directory target/dev-overlays/browser-web/qemu-firefox-startup \
  --boot-timeout 360 --smp 4
```

Expected ordered markers: `basic`, `x-socket-ready`, `firefox-exec`, and `marionette`, followed by `STARTUP_PROFILE_DONE`. QEMU must exit after the sample and leave no running `qemu-system-riscv64` process.

- [ ] **Step 2: Classify a failed sample before changing code**

Run:

```bash
rg -n 'STARTUP_PROFILE|BOOT_|ASTERINAS_FIREFOX|A_WEB_|panic|ERROR|FAIL|not implemented|unsupported' \
  target/dev-overlays/browser-web/qemu-firefox-startup/startup.serial.log
```

Expected: either all four ordered startup markers, or one earliest missing boundary with bounded diagnostics. Do not start a second QEMU run until that boundary has a testable explanation.

### Task 3: Apply only a demonstrated compatibility fix

- [ ] **Step 1: Write one failing regression test for the earliest missing boundary**

Add a focused test to `tools/riscv/tests/test_debian_browser_web.py` that feeds the observed transcript or wrapper state into the existing classifier and requires the precise diagnostic/behavior. Run that single test and verify it fails for the expected missing behavior, not for a fixture error.

- [ ] **Step 2: Implement the smallest wrapper, evidence, or kernel fix**

Modify only the producer responsible for the failed boundary. Do not relax the requirement for a live Firefox parent, content process, X11 window, or Marionette listener; do not disable certificate checks, Firefox sandbox options, or security evidence.

- [ ] **Step 3: Run focused and aggregate GREEN checks**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_browser_web -v
tools/riscv/firefox_fast_check.sh
git diff --check
```

Expected: all tests pass and the fast check emits `FIREFOX_FAST_CHECK_PASS`.

- [ ] **Step 4: Reapply only changed guest files through the development overlay**

Run the existing `browser-web` overlay builder against the frozen base image. Expected: the base image SHA-256 remains unchanged, the derived image/runtime digest changes only when an installed guest file changes, and no package download or debootstrap occurs.

- [ ] **Step 5: Repeat Task 2 once**

Expected: all four ordered startup markers pass. Retain the first failing and final passing serial logs under separate output directories.

### Task 4: Review the remote Firefox diagnostic candidate

- [ ] **Step 1: Compare commit `27113bdc2` against the observed QEMU failure**

Run:

```bash
git show --check 27113bdc2
git diff --stat main...origin/codex/megrez-desktop-only
```

Expected: the candidate is absorbed only if its bounded panic tail, browser-content failure marker, capability-progress marker, or proxy isolation directly closes an observed evidence gap. Otherwise leave it unmerged.

- [ ] **Step 2: Verify the candidate before integration**

Run its focused `test_debian_browser_web` suite in isolation, review the five-file diff against the Asterinas maintainability/development/security guidelines, then cherry-pick the single commit only if both checks pass.

### Task 5: Prepare the physical run without touching the board

- [ ] **Step 1: Build a distinct Megrez Sv48 kernel**

Run the repository's Megrez kernel target with `TARGET_ARCH=riscv64 SMP=4` and without `FEATURES=riscv_sv39_mode`. Store it under a board-qualified path; never overwrite or reuse the QEMU Sv39 artifact as board evidence.

- [ ] **Step 2: Run the physical gate's dry-run/preflight**

Validate serial device ownership, host NIC/address, board IPv4 availability, U-Boot command lengths, kernel/DTB/initramfs/rootfs hashes, output-directory freshness, fixture/proxy listener binding, and recovery timeout. Expected: no serial writes and no board reboot during preflight.

### Task 6: Run one controlled Megrez desktop/network/Firefox-window validation

- [ ] **Step 1: Arm serial capture before boot**

Use the existing board-session/gate tool with one owner for the serial device. Require a finite boot deadline, artifact hashes in `result.json`, and the configured software-reboot cleanup path.

- [ ] **Step 2: Validate ordered physical boundaries**

Require, in order: Asterinas boot, Debian/systemd basic target, Xorg socket, Openbox/terminal desktop, GMAC link/address/route/DNS/HTTP/HTTPS evidence, Firefox exec, live Firefox parent/content process, and an X11 Firefox window. Marionette readiness is recorded when available but full Baidu DOM/search is not required for this startup-precondition milestone.

- [ ] **Step 3: Stop on the first failure and recover once**

On failure, retain the serial tail and structured classification, request the existing software reboot, and release serial ownership. Do not loop boots, wait for the old 1800-second watchdog, or request a physical reset unless the board is unreachable after the bounded software-recovery path.

### Task 7: Verify and integrate

- [ ] **Step 1: Run final local verification**

Run:

```bash
tools/riscv/firefox_fast_check.sh
python3 -m unittest tools.riscv.tests.test_megrez_gmac_gate tools.riscv.tests.test_megrez_board_session -v
git diff --check
```

Expected: all focused tests pass. Report QEMU and board evidence paths and exact artifact hashes; distinguish startup compatibility from full Baidu compatibility.

- [ ] **Step 2: Commit only source and documentation**

Stage the reviewed source/tests/docs explicitly. Do not stage `target/`, rootfs images, serial logs, screenshots, or the primary worktree's unrelated dirty files.
