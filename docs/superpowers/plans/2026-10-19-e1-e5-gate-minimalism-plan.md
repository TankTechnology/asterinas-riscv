# E1–E5 Gate Minimalism Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove obsolete desktop/browser gate paths, centralize the remaining E1–E5 entry points, and replace repeated full-boot diagnosis with bounded probes and reusable rootfs overlays.

**Architecture:** Keep `docs/porting` as the evidence authority. Keep one small Python gate runtime and one immutable rootfs input; make E1–E5 the only active acceptance paths. Move systemd and Firefox diagnosis into short probes that emit phase receipts without starting the full desktop scenario.

**Tech Stack:** Python 3 standard library, POSIX shell, Make, ext2/debugfs overlays, QEMU, existing `GateConfig`/`GateResult` protocol.

---

### Task 1: Freeze the deletion manifest

**Files:**
- Create: `tools/riscv/refactor_reference_scan.py`
- Create: `tools/riscv/tests/test_refactor_reference_scan.py`
- Create: `docs/porting/refactor-deletions.json`
- Create: `docs/porting/refactor-reference-report.md`
- Read: `Makefile`
- Read: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Read: `tools/riscv/debian/rootfs/build_stage1.sh`

- [ ] **Step 1: Generate a complete reference report**

Run:

```bash
PYTHONPATH=tools/riscv python3 tools/riscv/refactor_reference_scan.py \
  --root tools/riscv/debian/rootfs \
  --tests tools/riscv/tests \
  --makefile Makefile \
  --output docs/porting/refactor-reference-report.md
```

The report must list every candidate with: path, tracked/untracked state, code references, Make targets, rootfs overlay references, and evidence-only references. A candidate with a dynamic reference is `keep` until manually classified.

- [ ] **Step 2: Encode the reviewed deletion set**

Write `docs/porting/refactor-deletions.json` with this shape:

```json
{
  "schema": 1,
  "keep": ["..."],
  "delete": [
    {"path": "...", "reason": "...", "replacement": "...", "references_checked": true}
  ],
  "retain_docs": ["docs/porting/evidence", "docs/porting/receipts"]
}
```

The initial delete list must include only paths whose report has no active E1–E5 caller. It must not include `kernel/`, `ostd/`, `docs/porting/evidence/**`, `docs/porting/receipts/**`, rootfs manifests, or effective build inputs.

- [ ] **Step 3: Test the manifest is safe**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest tools.riscv.tests.test_refactor_reference_scan -v
python3 -m json.tool docs/porting/refactor-deletions.json >/dev/null
```

Expected: all candidate paths exist, no keep/delete overlap, and no protected path appears in `delete`.

- [ ] **Step 4: Commit only the manifest and report**

```bash
git add tools/riscv/refactor_reference_scan.py \
  tools/riscv/tests/test_refactor_reference_scan.py \
  docs/porting/refactor-deletions.json \
  docs/porting/refactor-reference-report.md
git commit -m "tools: record E1-E5 refactor deletion manifest"
```

### Task 2: Introduce one atomic receipt writer

**Files:**
- Create: `tools/riscv/debian/rootfs/receipt_writer.py`
- Modify: `tools/riscv/debian/rootfs/gate_protocol.py`
- Modify: `tools/riscv/debian/rootfs/rootfs_gate.py`
- Test: `tools/riscv/tests/test_receipt_writer.py`

- [ ] **Step 1: Write failing atomic-write tests**

The tests must assert that `start()` creates `status="running"`, `update()` preserves the schema and replaces the file atomically, and `finish()` records `finished_utc`, `recovery`, and `reason` even when the command exits non-zero.

- [ ] **Step 2: Implement the writer**

Implement `ReceiptWriter(path, base)` with `start()`, `update(**fields)`, and `finish(status, **fields)`. Write JSON to `path.with_suffix(path.suffix + ".tmp")`, flush, call `os.fsync()`, then `os.replace()` and fsync the parent directory. Reject status transitions after a terminal state.

- [ ] **Step 3: Adapt the common gate runtime**

Use the writer before launching QEMU and update it from timeout, signal, child exit, and cleanup paths. Keep existing receipt fields so historical readers remain compatible.

- [ ] **Step 4: Run focused tests**

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_receipt_writer \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsGateRuntimeTests -v
```

Expected: PASS; no QEMU is launched.

### Task 3: Centralize QEMU and rootfs inputs

**Files:**
- Create: `tools/riscv/debian/rootfs/gate_inputs.py`
- Modify: `tools/riscv/debian/rootfs/rootfs_gate.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_gate.py`
- Modify: `tools/riscv/debian/rootfs/firefox_drm_scene_gate.py`
- Test: `tools/riscv/tests/test_gate_inputs.py`

- [ ] **Step 1: Define immutable input types**

Add `GateInputs` for kernel, U-Boot, DTB, Stage1, root image, manifest, package lock, checksums and scene HTML. Add `BootPolicy` for `smp`, `boot_timeout`, `command_timeout`, `nic`, and `display`. Validate every path and compute hashes once.

- [ ] **Step 2: Replace duplicated boot argument construction**

Make `desktop_m4_gate.py` and the retained Firefox gate consume `GateInputs` and one `BootPolicy`. Keep `/dev/vdb`, `-nic none`, and the current U-Boot handoff as defaults; callers may override only through the policy object.

- [ ] **Step 3: Add overlay reuse**

Add `materialize_overlay(base_dir, spec, output_dir)` cache validation. If the base image and overlay manifest hashes match, reuse the existing image; otherwise create a new overlay and write its manifest before use.

- [ ] **Step 4: Test cache identity and argument equality**

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_gate_inputs \
  tools.riscv.tests.test_debian_dev_overlay \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsCommandTests -v
```

### Task 4: Add the short systemd ExecStart boundary probe

**Files:**
- Create: `tools/riscv/debian/rootfs/systemd_exec_boundary_probe.py`
- Create: `tools/riscv/debian/rootfs/systemd_exec_boundary.service`
- Create: `tools/riscv/debian/rootfs/systemd_exec_boundary.sh`
- Create: `tools/riscv/tests/test_systemd_exec_boundary_probe.py`
- Modify: `tools/riscv/debian/rootfs/dev_overlay.py`

- [ ] **Step 1: Write classifier tests**

Cover: preflight reached, `ExecStart` reached, child ready, child exit, timeout, and missing phase. A timeout must produce a terminal receipt and never be interpreted as desktop failure.

- [ ] **Step 2: Implement the guest probe**

The service writes the five `probe=systemd-exec` markers to stderr and exits. Generate four variants from one template: root, `User=asterinas`, `SupplementaryGroups=video input tty render`, and shell-script exec. Each variant uses a 10-second timeout.

- [ ] **Step 3: Implement the host runner**

Reuse the fixed kernel/Stage1/root image, inject only the probe service, and stop at the first terminal marker. Persist serial, process state and receipt in one directory.

- [ ] **Step 4: Run one bounded QEMU probe**

```bash
python3 -m tools.riscv.debian.rootfs.systemd_exec_boundary_probe \
  --variant user --boot-timeout 10 --output-directory /mnt/mydisk/systemd-exec-boundary-r1
```

Expected: a terminal receipt in at most 10 seconds; no Firefox, Xorg, network, or screenshot service starts.

### Task 5: Remove obsolete gate implementations and tests

**Files:**
- Modify: `Makefile`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/debian/rootfs/README.md`
- Delete: every path listed in `docs/porting/refactor-deletions.json`
- Test: retained E1–E5 unit test modules

- [ ] **Step 1: Remove obsolete Make targets**

Delete M5–M9, browser daily-use, Baidu, network-pressure, Firefox source-builder and duplicate physical-graphics targets. Keep E1–E5 targets and generic build/unit targets. Every removed target must appear in the deletion manifest.

- [ ] **Step 2: Remove obsolete rootfs payloads**

Delete only payloads whose source is in the manifest. Update the expected archive list in `build_stage1.sh` and `build_rootfs.sh` so the builder no longer copies removed scripts/services.

- [ ] **Step 3: Remove matching tests**

Delete only tests that import removed modules or assert removed Make targets. Keep tests for `desktop_m4_gate`, DRM contracts, U-Boot contracts, receipt verification, and management-chain parsing.

- [ ] **Step 4: Run static reference checks**

```bash
PYTHONPATH=tools/riscv python3 tools/riscv/refactor_reference_scan.py \
  --root tools/riscv/debian/rootfs --tests tools/riscv/tests \
  --makefile Makefile --verify docs/porting/refactor-deletions.json
git grep -nE 'desktop_m[56789]|browser_m5|baidu|cross-build|builder' \
  Makefile tools/riscv/debian/rootfs tools/riscv/tests
```

Expected: only retained documentation or explicit compatibility references remain.

### Task 6: Reduce the active test surface

**Files:**
- Modify: `Makefile`
- Modify: `tools/riscv/README.md`
- Modify: `docs/porting/STATUS.md` generator inputs
- Test: `tools/riscv/tests/test_makefile_e1_e5.py`

- [ ] **Step 1: Define the active target list**

Retain exactly five gate targets plus base build, unit-test, and verification targets. The test asserts that no M5–M9 or daily-use target is exported.

- [ ] **Step 2: Document the fast loop**

Document: `verify.py` → short systemd probe → E1/E2 → E3 → E4 → E5. State that rootfs images are reused by manifest hash.

- [ ] **Step 3: Run retained unit tests**

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_makefile_e1_e5 \
  tools.riscv.tests.test_qemu_uboot_contracts \
  tools.riscv.tests.test_drm_render_node_gate \
  tools.riscv.tests.test_drm_gem_gate \
  tools.riscv.tests.test_drm_virgl_param_gate \
  tools.riscv.tests.test_firefox_webrender_gate -v
```

### Task 7: Produce the refactor evidence and bottleneck report

**Files:**
- Create: `docs/porting/evidence/2026-10-19-gate-minimalism.md`
- Create: `docs/porting/receipts/2026-10-19-gate-minimalism.json`
- Modify: `docs/porting/STATUS.md` via `verify.py`

- [ ] **Step 1: Record deletion counts and retained surface**

Record files deleted by category, active targets before/after, and protected history paths.

- [ ] **Step 2: Record measured bottlenecks**

Use the systemd probe and existing r66/r70 serials to report median phase times and the exact missing phase. Keep PAM/TTY, credentials, and script exec as separate hypotheses.

- [ ] **Step 3: Verify evidence**

```bash
python3 docs/porting/verify.py
git diff --check
```

Expected: historical E1/E2/E4 status remains unchanged; E3/E5 remain explicitly unresolved where their receipts say so; the new refactor receipt is terminal and hash-bound.

