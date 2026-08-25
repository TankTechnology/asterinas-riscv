# Megrez Asterinas Debian Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a restart-safe Asterinas initramfs installer for the frozen Debian ext2 image on Megrez partition 2.

**Architecture:** A focused Python builder parses and rewrites raw `newc`, splits the validated image into deterministic gzip chunks, and generates a guarded PID1 shell protocol. The board boots this payload with the already-gated Asterinas MMC writer; Linux only stages immutable boot files.

**Tech Stack:** Python 3 standard library, raw `newc`, gzip, BusyBox shell, Asterinas SDHCI block device.

---

### Task 1: Freeze archive and runtime contracts

**Files:**
- Create: `tools/riscv/tests/test_megrez_debian_installer.py`
- Create: `tools/riscv/debian/rootfs/megrez_installer.py`

- [ ] Write tests for `newc` parsing, path rejection, chunk planning, guarded init text, deterministic output, and failed-output preservation.
- [ ] Run `python3 -m unittest tools.riscv.tests.test_megrez_debian_installer -v` and record the missing-module RED.
- [ ] Implement the smallest parser, writer, builder CLI, and generated init protocol that satisfy the tests.
- [ ] Re-run focused tests, `py_compile`, Ruff, and `git diff --check`.
- [ ] Commit as `build(riscv): package Megrez Debian installer`.

### Task 2: Build and inspect the real installer

**Files:**
- Output only: `target-ubuntu/megrez-m2b/debian-installer.cpio`

- [ ] Verify the frozen root contract and its existing SHA-256.
- [ ] Build from the current 29 MiB raw RISC-V BusyBox initramfs with 32 MiB chunks.
- [ ] Inspect exact archive entries, modes, manifest, total size, and repeat-build SHA-256.
- [ ] Confirm the result fits the 500 MiB boot partition before staging.

### Task 3: Run the real Asterinas installer

**Files:**
- Evidence only: `target-ubuntu/megrez-m2b/installer-*.log`

- [ ] Stage the immutable archive and verify its SHA-256 on the boot filesystem.
- [ ] Boot the existing Asterinas kernel with both exact write and image-hash guards.
- [ ] Observe bounded per-chunk write/readback markers without passive waiting.
- [ ] Require the final 1 GiB SHA-256 to match the frozen image.
- [ ] Reboot without the write flag and verify the installed ext2 label read-only.

### Task 4: Boot Debian through Stage1

**Files:**
- Evidence only: `target-ubuntu/megrez-m2b/debian-first-boot.log`

- [ ] Boot the committed Stage1 initramfs and current Asterinas kernel read-only.
- [ ] Require `DEBIAN_ROOTFS_READY`, Debian release identity, RISC-V Bash execution, and a persistence nonce.
- [ ] Reboot once more and require the same nonce.
- [ ] Record the remaining kernel/syscall blockers without claiming desktop readiness.

