# Firefox RISC-V Runtime Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and statically admit a separate Debian `browser-m5` Firefox rootfs, then add a bounded QEMU startup probe that reports readiness or a classified cold-start failure without waiting through the full Marionette content timeout.

**Architecture:** Reuse the existing signed rootfs builder and Firefox M5 runtime scripts. Add one fail-closed stage-root checker that runs before image publication, and one host-side startup classifier/orchestrator that stops at the first Firefox/Xorg/Marionette readiness boundary. The existing full QEMU M5 gate remains the later content acceptance gate; the installed NetSurf image and Megrez board are not modified by this milestone.

**Tech Stack:** Bash rootfs builder, Python `unittest`, Python gate runtime, Debian riscv64 ELF inspection, QEMU RISC-V `virt` with SMP=4/Sv39.

---

### Task 1: Add a fail-closed Firefox stage-root checker

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_m5_rootfs_check.py`
- Create: `tools/riscv/tests/test_debian_browser_m5_rootfs.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh:906-915`

- [ ] **Step 1: Write the failing checker tests**

Create a temporary stage root with a Debian status file and the Firefox launcher,
then assert that `check_root()` accepts the complete required file set and
rejects (a) missing Firefox, (b) an installed `netsurf-gtk`, (c) a non-riscv64
Firefox ELF, and (d) a launcher containing `--no-sandbox`. Patch only the
checker’s ELF inspection helper in the synthetic test; all path, package, and
launcher checks must execute against real temporary files.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_browser_m5_rootfs -v
```

Expected: import failure because `browser_m5_rootfs_check.py` does not exist.

- [ ] **Step 3: Implement the minimal checker**

Implement `check_root(root: Path) -> str` with these exact checks:

```text
var/lib/dpkg/status: firefox-esr installed; netsurf-gtk absent
usr/bin/firefox-esr: symlink target ../lib/firefox-esr/firefox-esr
usr/lib/firefox-esr/firefox-esr: executable RISC-V ELF
usr/share/asterinas/browser-m5/index.html and browser-m5.webm: regular files
usr/lib/asterinas/browser-m5-marionette-gate: executable
usr/lib/asterinas/browser-m5-firefox: executable, no --no-sandbox token
usr/lib/asterinas/browser-m5-window-observer: executable
usr/lib/asterinas/browser-m5-network-observer: executable
etc/systemd/system/asterinas-browser-m5.service: regular file containing
  User=asterinas, PrivateNetwork=yes, NoNewPrivileges=yes, and --marionette
```

Return exactly `FIREFOX_M5_ROOTFS_PASS firefox=riscv64 sandbox=normal assets=local`
on success and `FIREFOX_M5_ROOTFS_FAIL reason=...` on CLI failure. Reject
symlink escapes and oversized probe assets before checking the ELF.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the same unittest command; require all tests to pass.

- [ ] **Step 5: Wire the checker into publication**

In `configure_and_normalize_rootfs`, after the browser M5 assets and services
are installed and before cleanup removes build-only files, invoke the checker
for `browser-m5` and write its exact output to
`usr/share/asterinas/browser-m5-rootfs-static.log`. Abort the build if the
output is not the exact PASS line. Do not run it for other profiles.

- [ ] **Step 6: Verify the builder wiring**

Run:

```bash
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
python3 -m unittest tools.riscv.tests.test_debian_browser_m5_rootfs
```

Expected: syntax clean and all checker/wiring tests pass.

### Task 2: Build and audit the immutable Firefox rootfs

**Files:**
- Generate (ignored): `target/debian-riscv/browser-m5/rootfs/*`
- Inspect: `tools/riscv/debian/rootfs/browser_m5_rootfs_check.py`

- [ ] **Step 1: Check the container tool boundary**

Run the builder’s `--print-tools` and verify that the pinned Asterinas container,
not the host shell, supplies `debootstrap`, `qemu-riscv64-static`, `ffmpeg`,
`ffprobe`, `gpgv`, and enabled RISC-V binfmt.

- [ ] **Step 2: Build in the pinned container**

Run the existing builder with the TUNA base mirror and its fixed default output:

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD:/root/asterinas" \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'cd /root/asterinas && tools/riscv/debian/rootfs/build_rootfs.sh --profile browser-m5'
```

The command may fetch packages, but it must remain bounded by the builder’s
phase logs and clean its private workspace on interruption.

- [ ] **Step 3: Verify the published artifact set**

Require `debian-root.ext2`, `rootfs-manifest.json`, `packages.lock`,
`source-metadata/InRelease`, `source-metadata/Security-InRelease`, and
`source-metadata/package-checksums`. Load the manifest and require profile
`browser-m5`, schema 6, architecture `riscv64`, and Firefox’s two signed source
roles. Run the static checker against an extracted stage copy and record the
image and manifest SHA-256 values in a local evidence file.

### Task 3: Add a bounded QEMU cold-start probe contract

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_m5_startup_probe.py`
- Create: `tools/riscv/debian/rootfs/browser_m5_startup_evidence.sh`
- Create: `tools/riscv/tests/test_debian_browser_m5_startup_probe.py`
- Modify: `Makefile`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/debian/rootfs/README.md`

- [ ] **Step 1: Write failing transcript classification tests**

Define ordered startup milestones for kernel entry, systemd session, Xorg fbdev,
Firefox process, Marionette loopback, and the existing Navigator visible marker.
Test that a transcript returns `ready`, classifies an explicit Firefox exit,
classifies an Xorg/input failure, rejects out-of-order markers, and reports
`timeout` when no readiness marker appears. Include a JSON result schema with
`passed`, `reason`, `checkpoint_seconds`, and `milestones`.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_browser_m5_startup_probe -v
```

Expected: import failure for the new probe module.

- [ ] **Step 3: Implement the guest readiness helper**

Install `browser_m5_startup_evidence.sh` as a oneshot systemd service. It polls
the Firefox service main PID, `/proc` command line/status, Xorg FBDEV and evdev
markers, MarionetteActivePort, and the window observer's NavigatorWindowReady
file. It emits one exact `DEBIAN_BROWSER_M5_STARTUP_READY` marker or one
classified `DEBIAN_BROWSER_M5_STARTUP_FAIL` marker within 600 seconds; it never
creates a content session.

- [ ] **Step 4: Implement transcript classifier and bounded QEMU probe**

Implement a pure `classify_startup(transcript: bytes, expected_release: str)`
function plus a CLI that launches the existing pinned QEMU lifecycle with
`BrowserM5StartupOperations`. The adapter must stop at the startup marker and
publish a small JSON result; it must never claim content success. Use a default
checkpoint budget of 600 seconds and accept only positive integer checkpoint
values no greater than 600. Reuse the existing artifact pinning and process
cleanup instead of opening arbitrary transcript paths.

- [ ] **Step 5: Verify GREEN and safety properties**

Run the focused tests, `python3 -m py_compile` on the probe, and `git diff --check`.
Require explicit failure for malformed evidence, duplicate milestones, and a
Firefox `--no-sandbox` diagnostic line.

- [ ] **Step 6: Add a Make target and documentation**

Add `test_riscv_debian_browser_m5_startup_probe` with the same immutable artifact
arguments as the full gate and a fixed `--boot-timeout 600`. Document that this
target is the short pre-gate; the existing
`test_riscv_debian_browser_m5_qemu_gate` remains the full offline content gate.

### Task 4: Run the QEMU gates only after F0 passes

**Files:**
- Generate (ignored): `target/debian-riscv/browser-m5/qemu-gate/*`
- Inspect: `docs/porting/evidence/2026-08-31-megrez-debian-browser-quality.md`

- [ ] **Step 1: Run the short classifier against a synthetic transcript**

Require `ready` and each failure class before starting a real QEMU process.

- [ ] **Step 2: Run the existing full QEMU browser M5 gate**

Use the newly built Firefox rootfs with SMP=4/Sv39 and the existing compiled
kernel/U-Boot/DTB/Stage1 artifacts. Keep the gate’s private loopback namespace,
Marionette DOM markers, sandbox checks, and screenshot evidence. Stop on the
first classified failure; do not wait for a board reset or remote CI.

- [ ] **Step 3: Record a pass or first failure**

If it passes, record the exact rootfs/plan hashes and all M5 markers. If it
fails, record only the first boundary and attach process/serial diagnostics;
do not proceed to the physical board or online Firefox profile.

- [ ] **Step 4: Commit the milestone**

Run the focused and full relevant unit tests, `git diff --check`, then commit
the checker/probe/docs as one focused change and push the current branch. Do not
modify the installed NetSurf rootfs or remote `main` in this task.
