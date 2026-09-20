# Megrez Fast Desktop Boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide one bounded Megrez command that starts the staged Asterinas desktop, proves a visible Firefox window and serial debug console within five minutes, disarms the guest watchdog only after readiness, and otherwise returns to U-Boot without a physical reset.

**Architecture:** A host-side `prepare` command publishes one immutable kernel/Stage1/DTB generation to RockOS partition 3, while a separate `start` command verifies and loads that generation directly from U-Boot. The kernel owns a boot-time, one-way-disarm recovery watchdog exposed through a capability-gated proc sysctl. Stage1 installs a transient systemd readiness service; the guest disarms its own watchdog only after the desktop contract passes, and the host independently records serial phases and read-only admission evidence.

**Tech Stack:** Rust kernel and procfs, C Stage1 generator, POSIX shell guest controls, Python 3 host orchestration and `unittest`, GNU Make, persistent project Docker, U-Boot/eMMC, systemd, Xorg/Openbox/Firefox.

---

## Task 1: Capture the current root-handoff failure with serial-visible logging

**Files:**

- Read: `target/firefox-daily-use-physical/plan-namespace.json`
- Read: `tools/riscv/megrez_physical_graphics.py`
- Read: `tools/riscv/megrez_menu_board.py`
- Create evidence: `target/megrez-desktop-boot/diagnostic-current-root/serial.log`
- Create evidence: `target/megrez-desktop-boot/diagnostic-current-root/result.json`

- [ ] Confirm the serial port is free, the board is at a fresh U-Boot prompt, and the exact kernel, Stage1, DTB, and installed root identities match the frozen plan and prior inventory.
- [ ] Boot those exact artifacts once with `console=ttyS0 loglevel=info asterinas.reboot_after=300`, preserving all existing Stage1 and root-init arguments.
- [ ] Record the ordered `DEBIAN_STAGE1_PROGRESS`, `DEBIAN_ROOTFS_FAIL`, kernel error, reboot, OpenSBI, U-Boot, and prompt markers without retrying commands blindly.
- [ ] Require software recovery to a fresh U-Boot prompt no later than 360 seconds after kernel entry; do not request a physical reset if the attempt fails.
- [ ] Write canonical diagnostic JSON containing artifact hashes, exact bootargs, the last completed Stage1 operation, failure text, kernel-to-recovery duration, and SHA-256 of `serial.log`.
- [ ] Use the first failing operation as the evidence-backed root cause. Add the smallest regression test to the affected Stage1 or host module before changing behavior; if the boot succeeds, retain the diagnostic and proceed without inventing a root-handoff fix.

## Task 2: Add the kernel's one-way watchdog disarm primitive

**Files:**

- Modify: `kernel/src/boot_reboot.rs`

- [ ] Add failing kernel tests for: watchdog disabled, watchdog armed, first disarm, repeated disarm, and a stale timer callback after disarm.
- [ ] Run the focused kernel tests in the persistent container and confirm the new assertions fail for the intended missing API:

  ```bash
  tools/docker/run_dev_container.sh -- make ktest KTEST_FILTER=boot_reboot TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
  ```

- [ ] Add `pub(crate)` status and disarm APIs. Publish disarm with `Release`, read armed state with `Acquire`, preserve the frozen deadline, and return whether this call performed the first transition.
- [ ] Make the timer callback perform its acquire read immediately before rearming/restarting so a stale callback becomes a no-op after disarm.
- [ ] Emit `ASTERINAS_SOFTWARE_REBOOT_DISARMED` exactly once on the first successful disarm.
- [ ] Re-run the focused kernel tests and retain their output.
- [ ] Commit only the kernel primitive and its tests.

## Task 3: Expose the watchdog as a capability-gated proc sysctl

**Files:**

- Create: `kernel/src/fs/fs_impls/procfs/sys/kernel/asterinas_reboot_watchdog.rs`
- Modify: `kernel/src/fs/fs_impls/procfs/sys/kernel/mod.rs`
- Reference: `kernel/src/fs/fs_impls/procfs/sys/kernel/dmesg_restrict.rs`
- Reference: `kernel/src/fs/fs_impls/procfs/template/file.rs`

- [ ] Add failing procfs tests covering readback, missing `CAP_SYS_ADMIN`, nonzero offset, empty/oversized input, values other than exactly `0`, trailing non-whitespace data, accepted sysctl whitespace, first disarm, and idempotent disarm.
- [ ] Run the focused procfs tests in the persistent container and confirm the intended failures.
- [ ] Implement `/proc/sys/kernel/asterinas_reboot_watchdog`: read `1\n` when armed and `0\n` otherwise; accept only a zero-at-offset-zero write by a task with `CAP_SYS_ADMIN`; never arm, rearm, or extend a deadline.
- [ ] Register the file in `/proc/sys/kernel` with the same ownership and mode conventions as existing writable kernel sysctls.
- [ ] Re-run focused procfs tests, the `boot_reboot` kernel tests, `cargo fmt --check`, and targeted Clippy in the persistent container.
- [ ] Commit the proc interface and tests separately from Stage1 and host changes.

## Task 4: Generate the transient guest readiness service

**Files:**

- Modify: `tools/riscv/debian/rootfs/stage1_debug_console.c`
- Modify: `tools/riscv/debian/rootfs/stage1_debug_console.h`
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/debian/rootfs/physical_graphics_control.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add failing tests asserting that Stage1 writes a transient `/newroot/run/systemd/system/asterinas-desktop-ready.service`, enables it from the runtime target, and points it only at the Stage1 tools bind mount.
- [ ] Add failing shell-fixture tests for each readiness predicate: debug console inactive, missing `/dev/fb0`, missing X11 socket, non-fbdev Xorg, absent Openbox, wrong Firefox user, absent visible Firefox window, successful watchdog disarm/readback, and exact ready marker.
- [ ] Add a `startup-ready` action to `physical_graphics_control.sh` with a bounded shared deadline. It must print an exact failure reason and leave the watchdog armed for every rejected predicate.
- [ ] On success, write `0` once to `/proc/sys/kernel/asterinas_reboot_watchdog`, require readback `0`, emit ordered display/firefox/watchdog phases, and finish with `ASTERINAS_DESKTOP_BOOT_READY` on `/dev/console`.
- [ ] Extend Stage1's runtime unit generation and archive build inputs without modifying the persistent root filesystem.
- [ ] Run the focused red/green tests, then the complete rootfs unit target in the persistent container:

  ```bash
  tools/docker/run_dev_container.sh -- make test_riscv_debian_rootfs_unit
  ```

- [ ] Build Stage1 twice and verify byte-for-byte deterministic output and the expected embedded service/control files.
- [ ] Commit the transient readiness service, control action, and tests.

## Task 5: Implement immutable `prepare` publication

**Files:**

- Create: `tools/riscv/megrez_desktop_boot.py`
- Create: `tools/riscv/tests/test_megrez_desktop_boot.py`
- Reference: `tools/riscv/megrez_boot_menu.py`
- Reference: `tools/riscv/megrez_menu_board.py`

- [ ] Write failing Python tests for canonical manifest serialization, generation identity, SHA-prefixed safe basenames, allowed partition-3 destination paths, rejection of traversal/symlinks/mismatched existing files, interrupted temporary publication, idempotent matching publication, and refusal to overwrite a different generation.
- [ ] Define a versioned manifest containing plan identity, expected root identity, artifact basename/size/SHA-256/CRC32, Stage1 protocol version, bootargs template, and generation SHA-256.
- [ ] Implement `python3 -m tools.riscv.megrez_desktop_boot prepare` using the existing serial/RockOS session abstraction: enter RockOS through software-controlled U-Boot, upload into a unique sibling temporary directory, verify every size and SHA-256 on the board, `sync`, atomically rename, and read back the final manifest.
- [ ] Keep all generations under `/home/debian/asterinas/boot/<generation-prefix>/`; do not write partition 1, partition 2, U-Boot environment, or delete an older generation.
- [ ] Ensure secrets are neither embedded in the manifest nor copied to retained logs.
- [ ] Run:

  ```bash
  tools/docker/run_dev_container.sh -- python3 -m unittest tools.riscv.tests.test_megrez_desktop_boot -v
  ```

- [ ] Commit the prepare implementation and unit tests.

## Task 6: Implement the bounded `start` state machine

**Files:**

- Modify: `tools/riscv/megrez_desktop_boot.py`
- Modify: `tools/riscv/tests/test_megrez_desktop_boot.py`
- Reference: `tools/riscv/megrez_physical_graphics.py`
- Reference: `tools/riscv/megrez_menu_board.py`

- [ ] Add failing tests for absent staging, U-Boot `ext4load mmc 1:3` commands, byte-count and CRC32 validation, forbidden transfer/install/network operations, required phase ordering, duplicate/skipped/malformed phase rejection, the 300-second ready deadline, the 360-second recovery deadline, boot-epoch detection, serial log hashing, and atomic final `result.json` publication.
- [ ] Implement `start` so it first validates the local plan and immutable manifest, then checks the three persistent files from U-Boot before `booti`; an unstaged generation must fail with an actionable `prepare` command.
- [ ] Load kernel, Stage1, and prepared DTB only from `mmc 1:3`; validate U-Boot byte count and CRC32 for each artifact before execution.
- [ ] Use serial-visible informational bootargs and `asterinas.reboot_after=300`. Track the ten ordered phases from `kernel-entered` through `desktop-ready` using host-monotonic timestamps.
- [ ] On success, perform only read-only debug-console probes for debug-console activity, X11 socket, Firefox PID/user, visible Firefox window, watchdog readback `0`, and boot ID. Do not let the host disarm the watchdog.
- [ ] On failure after kernel entry, keep observing until a new OpenSBI/U-Boot/prompt epoch is proven, or record bounded recovery failure at 360 seconds. Never request a physical reset.
- [ ] Publish `result.json` last and include stable reason, all identities/paths, phase timings, boot ID, Firefox PID, watchdog state, recovery evidence, and serial SHA-256.
- [ ] Run the complete host unit suite in the persistent container and commit the start implementation and tests.

## Task 7: Add stable Make entry points and operator documentation

**Files:**

- Modify: `Makefile`
- Modify: `tools/riscv/README.md`
- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `tools/riscv/tests/test_megrez_desktop_boot.py`

- [ ] Add failing command-construction tests for the documented default plan, serial device, output root, and explicit override variables.
- [ ] Add `prepare_riscv_megrez_desktop_boot` and `run_riscv_megrez_desktop` phony targets. The run target must invoke only `start`, never prepare, install, fixture, qualification, or performance commands.
- [ ] Document the two-command lifecycle, the five-minute contract, retained evidence paths, safe repeat-start behavior, explicit software reboot, and recovery troubleshooting based on the last serial phase.
- [ ] Name `run_riscv_megrez_desktop` as the normal daily startup path while preserving existing diagnostic/install tools as exceptional workflows.
- [ ] Run `--help`, the unit tests, `make -n` for both targets, Markdown link checks available in `make check`, and `git diff --check`.
- [ ] Commit Makefile and documentation changes.

## Task 8: Build and run the QEMU watchdog matrix

**Files:**

- Modify if required: `tools/riscv/tests/test_megrez_desktop_boot.py`
- Create evidence: `target/megrez-desktop-boot/qemu-success/`
- Create evidence: `target/megrez-desktop-boot/qemu-recovery/`

- [ ] Build the RISC-V kernel and Stage1 with the persistent caches:

  ```bash
  tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
  tools/docker/run_dev_container.sh --image asterinas/asterinas:0.18.0-20260702-riscv-rootfs --offline -- tools/riscv/debian/rootfs/build_stage1.sh --help
  ```

- [ ] Run a shortened QEMU success case in which all readiness predicates pass, the watchdog changes from `1` to `0`, the terminal marker appears, and the guest stays alive beyond the original deadline.
- [ ] Run a shortened QEMU failure case in which readiness is deliberately withheld, the watchdog remains `1`, and the next firmware/boot epoch appears inside the proportional recovery bound.
- [ ] Retain canonical results and serial logs for both cases; compare their artifact and protocol identities.
- [ ] Run all new unit/ktest targets plus the affected existing Megrez and rootfs suites. Fix regressions using test-first slices and commit any QEMU-only corrections.

## Task 9: Publish the generation and validate real hardware

**Files:**

- Create evidence: `target/megrez-desktop-boot/physical-prepare/`
- Create evidence: `target/megrez-desktop-boot/physical-failure/`
- Create evidence: `target/megrez-desktop-boot/physical-success-1/`
- Create evidence: `target/megrez-desktop-boot/physical-success-2/`

- [ ] Confirm the board begins at U-Boot and the serial port has no competing owner. Use software reboot for every transition; do not ask for a physical reset.
- [ ] Run `prepare` once for the freshly built kernel, Stage1, and DTB; independently inspect the partition-3 generation and verify board-side SHA-256 and sizes against the canonical manifest.
- [ ] Run one intentionally withheld-readiness start. Require the watchdog to remain armed and prove a fresh U-Boot prompt within 360 seconds of kernel entry.
- [ ] Run the normal start twice consecutively. Each run must prove debug console, framebuffer/Xorg fbdev, Openbox, expected-user Firefox, visible Firefox window, watchdog readback `0`, and terminal readiness within 300 seconds.
- [ ] After one passing run, wait beyond the original 300-second deadline and re-query boot ID, Firefox PID/window, and watchdog state to prove the same guest remains alive until explicit software reboot.
- [ ] End each cycle with an explicit guest or firmware software reboot and prove the board returns to a fresh U-Boot prompt.
- [ ] Compare the two passing `result.json` files for identical immutable generation/root identities and distinct boot IDs; retain all serial logs and checksums.
- [ ] If any run fails, diagnose from the last ordered phase, add a failing automated regression, implement the smallest fix, re-run affected container tests, and repeat only the failed physical case.

## Task 10: Integrate the pre-existing namespace and U-Boot prompt fixes

**Files:**

- Modify: `tools/riscv/debian/rootfs/physical_graphics_control.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `tools/riscv/megrez_physical_graphics.py`
- Modify: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [ ] Review the already-present `nsenter -m -n` daily-use namespace correction and the Ctrl-C U-Boot wakeup correction against their regression tests.
- [ ] Run the two focused tests first, then `make test_riscv_debian_rootfs_unit` and the existing physical-graphics unit target in the persistent container.
- [ ] Commit these corrections as independent changes rather than folding them into fast-boot feature commits.

## Task 11: Final verification and handoff

**Files:**

- Read: all files changed since `36a0f7a46`
- Read: all retained results under `target/megrez-desktop-boot/`

- [ ] Run `git diff --check`, repository formatting, targeted Clippy, all affected Python/rootfs/kernel tests, deterministic Stage1 build, and the final physical result validator from a clean command invocation.
- [ ] Inspect `git status --short` and preserve unrelated user-owned changes and evidence.
- [ ] Audit the implementation against every design goal and non-goal: one normal start path, no implicit install/transfer, immutable p3 generation, five-minute ready/recovery behavior, guest-only disarm, success persistence, serial observability, and no physical-reset dependency.
- [ ] Report exact test commands and outcomes, physical boot-to-ready/recovery durations, artifact identities, evidence paths, remaining limitations, and the single normal operator command.
