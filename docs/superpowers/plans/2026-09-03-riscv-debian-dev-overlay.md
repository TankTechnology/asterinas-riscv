# RISC-V Debian Development Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse one verified Debian RISC-V rootfs and materialize script/configuration-only development images in seconds without rerunning debootstrap or apt.

**Architecture:** Keep the signed rootfs and its package provenance immutable. A host-side Python tool validates a declarative overlay specification, reflink-copies the base ext2 image, replaces only pre-existing regular files with `debugfs`, verifies every injected byte and mode, and publishes a derived manifest beside the image. Existing QEMU gates consume the derived compatibility manifest, while `dev-overlay-manifest.json` preserves the explicit base-to-derived provenance.

**Tech Stack:** Python 3 standard library, `debugfs`, GNU `cp --reflink=auto`, ext2, `unittest`, Make.

---

### Task 1: Define and validate the overlay contract

**Files:**
- Create: `tools/riscv/debian/rootfs/dev_overlay.py`
- Create: `tools/riscv/tests/test_debian_dev_overlay.py`

- [x] **Step 1: Write failing tests for an exact schema, safe relative sources, canonical absolute destinations, octal modes, duplicate rejection, and profile matching.**
- [x] **Step 2: Run `python3 -m unittest tools.riscv.tests.test_debian_dev_overlay -v` and verify the module import fails.**
- [x] **Step 3: Implement immutable `OverlayFile` and `OverlaySpec` values plus `load_overlay_spec()`, rejecting unknown fields, symlinks, traversal, duplicates, and unsupported modes.**
- [x] **Step 4: Re-run the focused suite and verify the contract tests pass.**

### Task 2: Materialize and verify a derived ext2 image

**Files:**
- Modify: `tools/riscv/debian/rootfs/dev_overlay.py`
- Modify: `tools/riscv/tests/test_debian_dev_overlay.py`

- [x] **Step 1: Add a failing real-ext2 test that creates a small image, updates an existing file, checks the base remains unchanged, and verifies output bytes and mode through `debugfs`.**
- [x] **Step 2: Run the focused test and confirm it fails because materialization is absent.**
- [x] **Step 3: Implement adjacent temporary publication, reflink/sparse copy, existing-file checks, deterministic `debugfs` replacement, byte-and-mode verification, and rollback on failure.**
- [x] **Step 4: Add failing tests for a missing destination and unchanged published output after failure, then implement the fail-closed behavior.**
- [x] **Step 5: Re-run the focused suite and verify all materialization tests pass.**

### Task 3: Preserve provenance and expose the browser-web fast path

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_web_dev_overlay.json`
- Modify: `tools/riscv/debian/rootfs/dev_overlay.py`
- Modify: `tools/riscv/tests/test_debian_dev_overlay.py`
- Modify: `Makefile`

- [x] **Step 1: Add failing tests for the companion manifest, derived root hash, unchanged package identities, and the exact browser-web source-to-guest mapping.**
- [x] **Step 2: Implement the `materialize` CLI. It verifies the frozen base contract, writes a gate-compatible derived rootfs manifest with an `asterinas-dev-overlay` digest, copies unchanged package provenance, and writes `dev-overlay-manifest.json`.**
- [x] **Step 3: Add `make build_riscv_debian_browser_web_dev_overlay` with explicit base/output variables and no network access or package installation.**
- [x] **Step 4: Run the focused suite and `make -n build_riscv_debian_browser_web_dev_overlay` to verify the interface.**

### Task 4: Document, benchmark, and run the regression gate

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `Makefile`

- [x] **Step 1: Document which changes qualify for an overlay, the one-command browser-web workflow, artifact identities, and when a full rebuild remains mandatory.**
- [x] **Step 2: Register the focused suite in `test_riscv_debian_rootfs_unit`.**
- [x] **Step 3: Materialize from the existing 2-GiB browser-web base, record wall time, and verify both the base and derived contracts.**
- [x] **Step 4: Run the focused test, Debian rootfs unit gate, Python compilation/lint for new code, and `git diff --check`.**
- [x] **Step 5: Review the final diff against maintainability, correctness, security, hardware, and documentation guidelines before committing.**
