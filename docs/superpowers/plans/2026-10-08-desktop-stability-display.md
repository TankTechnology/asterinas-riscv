# Stable Desktop Display and Recoverable Hardware Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Deliver a software-fallback desktop with verified QEMU fbdev/DRM boundaries and a recoverable, single-step real-board display path, while excluding PowerVR.

**Architecture:** Integrate only evidence-backed commits into a clean branch. Centralize boot-cycle ownership, watchdog arming, nonce verification, bounded commands, and recovery parsing. Keep fbdev as the fallback while QEMU DRM/KMS is developed behind an explicit provider and fail-closed gate.

**Tech Stack:** Rust kernel and ktests, Python gate/model tests, POSIX shell rootfs evidence, QEMU/OpenSBI/U-Boot, RockOS serial control, optional bounded state-model checking.

---

### Task 1: Create a clean integration baseline

**Files:**
- Create: `docs/superpowers/evidence/2026-10-08-desktop-stability-baseline.md`
- Test: `git diff --check`, RISC-V kernel build, focused BIO ktest

- [ ] **Step 1: Create an isolated worktree from `origin/main`**

Run:

```bash
git worktree add /home/ubuntu/.config/superpowers/worktrees/asterinas/desktop-stability-integration -b codex/desktop-stability-integration origin/main
```

Expected: the new worktree is clean and based on `origin/main`; the existing dirty worktree is unchanged.

- [ ] **Step 2: Cherry-pick only the verified stability commits**

Cherry-pick the current BIO fix and the already reviewed RISC-V trap/CPU-local commits:

```bash
git cherry-pick 00481890d 50802f1bf 3e48d8c57 0c9a3c262
```

If a commit is already present, record it as already integrated and continue without resolving unrelated files.

- [ ] **Step 3: Run the baseline checks**

Run inside the persistent development container:

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
git diff --check
make ktest TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode CARGO_OSDK_TEST_ARGS="--target-arch riscv64 --scheme riscv bio_completion_is_published_after_callback"
```

Expected: kernel build succeeds; the focused `aster-block` test reports one passing test; no change is made to the physical board.

- [ ] **Step 4: Record artifact hashes and commit the baseline evidence**

Record the kernel Image, U-Boot, DTB, Stage1 initramfs, and test command in `docs/superpowers/evidence/2026-10-08-desktop-stability-baseline.md`, then commit only that evidence file.

---

### Task 2: Selectively integrate ext2 metadata hardening

**Files:**
- Modify: ext2 implementation files touched by commits `784f7d750360a7bf412cda058cfae28afc89f414` and `50571997c921a220b24ec11ce3efee0f0366e5da`
- Test: ext2 concurrent-write gate and offline `e2fsck`

- [ ] **Step 1: Inspect the two PR commits against the clean baseline**

Run:

```bash
git show --stat 784f7d750360a7bf412cda058cfae28afc89f414 50571997c921a220b24ec11ce3efee0f0366e5da
git diff origin/main...784f7d750360a7bf412cda058cfae28afc89f414 -- kernel/src/fs
```

Expected: only ext2 metadata serialization and staged-writeback paths are included; no DRM, boot-menu, or physical-control files are pulled in.

- [ ] **Step 2: Cherry-pick the ext2 commits**

```bash
git cherry-pick 784f7d750360a7bf412cda058cfae28afc89f414 50571997c921a220b24ec11ce3efee0f0366e5da
```

Resolve only ext2/VFS conflicts. Abort rather than accepting a conflict in control-channel or display code.

- [ ] **Step 3: Run ext2 correctness gates**

Run the repository's focused ext2 durability/concurrency gate, then check the resulting image offline:

```bash
python3 tools/riscv/ext2_durability_gate.py --output-dir target/ext2-stability-integration-$(date +%Y%m%d-%H%M%S)
e2fsck -fn <fresh-unmounted-ext2-image>
```

Expected: concurrent metadata operations complete, injected flush errors propagate, exact payloads survive clean shutdown, and `e2fsck -fn` exits 0. No physical power-cut claim is made.

- [ ] **Step 4: Commit only after the gates pass**

Commit the two ext2 changes and their evidence as one reviewed integration unit.

---

### Task 3: Centralize the bounded boot/recovery protocol

**Files:**
- Create: `tools/riscv/megrez_control_gate.py`
- Modify: `tools/riscv/megrez_board_session.py`, `tools/riscv/megrez_desktop_boot.py`, `tools/riscv/megrez_debug_desktop.py`
- Test: `tools/riscv/tests/test_megrez_control_gate.py`

- [ ] **Step 1: Add the failing state-machine tests**

The tests must reject a physical cycle that starts without an armed watchdog, retries an MMIO timeout, reuses a nonce, or reports success without a post-recovery serial reopen. They must accept exactly one bounded experiment followed by a fresh RockOS nonce and boot ID.

- [ ] **Step 2: Implement `BootCycle` and `run_bounded_cycle()`**

The record must contain artifact hashes, selected menu entry, nonce, watchdog deadline, experiment name, serial identity, and recovery state. The runner must expose callbacks only for device-specific commands and must own timeout, serial close/reopen, nonce generation, and recovery parsing.

- [ ] **Step 3: Make existing scripts thin profiles**

Replace duplicated retry and recovery code with calls to the common runner. Preserve the opt-in Asterinas root console and recreate it on every selected boot.

- [ ] **Step 4: Run the control-gate unit tests**

```bash
python3 -m unittest tools/riscv/tests/test_megrez_control_gate.py
python3 -m py_compile tools/riscv/megrez_control_gate.py tools/riscv/megrez_board_session.py tools/riscv/megrez_desktop_boot.py
```

Expected: all invalid transition tests fail closed and all valid recovery traces pass.

---

### Task 4: Make QEMU fbdev evidence deterministic

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m4_framebuffer_probe.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_evidence.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_gate.py`
- Test: `tools/riscv/tests/test_debian_rootfs.py` and new pure framebuffer tests

- [ ] **Step 1: Extract pure layout and pixel functions**

Add pure functions for BGR/RGB packing, stride bounds, clipping, color counting, and checksum calculation. They must reject dimensions above 8192, non-32-bpp layouts, short reads, and writes outside the reported buffer.

- [ ] **Step 2: Add red tests**

Cover valid `1280x1024x32` and `1920x1080x32` layouts, padded stride, BgrReserved packing, short reads, single-color rejection, and out-of-bounds rectangles.

- [ ] **Step 3: Implement the bounded write/restore probe**

Read and hash the original 4 KiB, write a fixed small color pattern, flush using the existing bounded mechanism, read and hash it, then restore the original bytes and report both hashes.

- [ ] **Step 4: Run the focused Python suite**

```bash
python3 -m unittest tools/riscv/tests/test_debian_rootfs.py
python3 -m py_compile tools/riscv/debian/rootfs/desktop_m4_framebuffer_probe.py tools/riscv/debian/rootfs/desktop_m4_gate.py
```

- [ ] **Step 5: Run the existing QEMU desktop gate**

Use the updated kernel with the existing ext2 image and an output directory on `/tmp`, then preserve only the small serial log/result JSON under `target/`. Require guest `FB_COLORS distinct>=3`, `READY`, and a successful software-reboot recovery. A host PPM with one color remains a failed scanout result.

---

### Task 5: Add bounded buffer/fence model verification

**Files:**
- Create: `tools/riscv/framebuffer_state_model.py`
- Create: `tools/riscv/tests/test_framebuffer_state_model.py`
- Create: `docs/superpowers/evidence/2026-10-08-framebuffer-state-model.md`

- [ ] **Step 1: Define the finite transitions**

Implement `BufferState` (`FREE`, `MAPPED`, `DRAWN`, `FLUSHED`, `SCANNING_OUT`, `RELEASED`) and `FenceState` (`PENDING`, `SIGNALED`, `FAILED`) with explicit allowed events.

- [ ] **Step 2: Add exhaustive bounded checks**

Enumerate every event sequence up to length 8 and assert that scanout never references released storage, release never precedes fence completion, and failed fences never produce successful flips.

- [ ] **Step 3: Add negative transition tests**

Each illegal transition must raise a typed model error. Record the number of explored traces and the invariants checked.

- [ ] **Step 4: Run the model and commit its evidence**

```bash
python3 -m unittest tools/riscv/tests/test_framebuffer_state_model.py
```

Expected: all bounded traces satisfy the invariants; the evidence file records the bound and result.

---

### Task 6: Add the QEMU DRM/KMS gate behind fbdev fallback

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_display_provider.sh`
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Create: `tools/riscv/debian/rootfs/desktop_drm_qemu_gate.py`
- Test: `tools/riscv/tests/test_debian_drm_qemu_gate.py`

- [ ] **Step 1: Add parser/classifier tests**

Require `/dev/dri/card0`, one connector and mode, dumb-buffer allocation, mmap, modeset, page-flip, vblank/fence completion, and clean release. Reject missing fences, released scanout buffers, and host/guest pixel disagreement.

- [ ] **Step 2: Add the DRM provider profile**

Make the provider explicit (`fbdev` or `drm`). A missing provider-specific configuration must fail before Xorg starts, while `fbdev` remains the default fallback.

- [ ] **Step 3: Run QEMU DRM only after fbdev passes**

Use a virtio-gpu QEMU device and a bounded local test client. Do not start Firefox. Require the same watchdog/recovery gate as the fbdev run.

- [ ] **Step 4: Record a separate DRM result**

A DRM failure must not change the default provider or block fbdev desktop boot. Preserve the result JSON, serial log, and artifact hashes.

---

### Task 7: Prepare the real-board fb0 and DRM read-only gates

**Files:**
- Modify: `tools/riscv/megrez_gmac_gate.py` only if the common control interface requires it
- Modify: `tools/riscv/megrez_desktop_boot.py`
- Create: `tools/riscv/megrez_display_gate.py`
- Test: `tools/riscv/tests/test_megrez_display_gate.py`

- [ ] **Step 1: Add a read-only resource gate**

Collect the display device-tree node, MMIO range, clock/reset/IRQ descriptions, framebuffer mode, and `/dev/fb0` metadata without touching GPU registers, DMA, network, or filesystem state.

- [ ] **Step 2: Add the protected fb0 cycle**

Arm the watchdog, verify the current RockOS control channel, select one Asterinas entry, read/hash the first 4 KiB, write one small fixed region, restore it, and stop. Never retry on a serial or MMIO timeout.

- [ ] **Step 3: Require recovery before declaring success**

Close/reopen the serial port, verify a fresh nonce, UID 0, boot ID, and RockOS/U-Boot recovery. If any item is missing, mark the run failed and block the next board run.

- [ ] **Step 4: Run only after QEMU fbdev and recovery gates are green**

No desktop load, Firefox, network pressure, DRM register access, or PowerVR activity is permitted in this stage.

---

### Task 8: Integrate and publish the verified bundles

**Files:**
- Create: `docs/superpowers/evidence/2026-10-08-desktop-stability-final.md`
- Modify: project `main` only after review of the clean integration branch

- [ ] **Step 1: Run the required checks**

Run the focused Rust ktest, Python unit suites, model exploration, RISC-V kernel build, QEMU fbdev gate, QEMU DRM gate if implemented, and the protected board gates that have hardware access.

- [ ] **Step 2: Review the diff by bundle**

Verify that no PowerVR files, unrelated network changes, credentials, default boot-menu changes, or uncontrolled retries are present.

- [ ] **Step 3: Fast-forward the project main branch**

Merge only the green bundles into main. Keep failed or experimental DRM/board artifacts in their own branch with explicit result records.

- [ ] **Step 4: Publish the final evidence**

Record exact commits, artifact hashes, test commands, QEMU result paths, board recovery states, and remaining limitations. Never describe a kernel-side checksum as proof of panel-visible pixels without an external capture or display CRC.
