# Debian M6 Browser Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture a foreground Baidu NetSurf window and separately classify the packaged JavaScript engine while Asterinas runs the Debian M5 desktop in QEMU.

**Architecture:** Add one bounded guest browser-evidence service and local HTML fixture to the existing M5 profile. A thin M6 host adapter reuses the M5 Asterinas/QEMU lifecycle, captures the remote page at the guest's remote marker, then captures the local JavaScript result and publishes both images with one classified status.

**Tech Stack:** Bash/systemd, `xdotool`, NetSurf/Duktape, Python `unittest`, QEMU HMP screendump, Debian RISC-V signed rootfs.

---

### Task 1: Freeze the guest browser evidence contract

**Files:**
- Create: `tools/riscv/tests/test_debian_m6_browser.py`
- Create: `tools/riscv/debian/rootfs/desktop_m6_browser_gate.py`
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `Makefile`

- [ ] **Step 1: Write the failing profile and classifier tests**

Require `xdotool` in the M5 requested and identity package tuples. Define the
stable remote marker, exactly one `limited-pass|disabled|failed` JavaScript
marker, and a final marker carrying the same status. Require all M5 and M4
markers before the M6 markers and reject missing, duplicate, reordered, or
mismatched status evidence.

- [ ] **Step 2: Run the focused test and record RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m6_browser -v
```

Expected: import failure because `desktop_m6_browser_gate` does not exist.

- [ ] **Step 3: Implement the minimal pure classifier contract**

Export:

```python
DESKTOP_M6_REMOTE_MARKER = (
    "DEBIAN_BROWSER_M6_REMOTE host=www.baidu.com foreground=active"
)
DESKTOP_M6_JAVASCRIPT_STATUSES = ("limited-pass", "disabled", "failed")

def classify_desktop_m6_browser(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    base = classify_desktop_m5_qemu(
        transcript, expected_debian_release=expected_debian_release
    )
    if not base.passed:
        return base
    # The implementation then requires one allowed status and a matching final
    # marker after DESKTOP_M6_REMOTE_MARKER, returning a stable GateResult.
    return GateResult(True, "pass", None)
```

The classifier first calls `classify_desktop_m5_qemu`, then validates one
JavaScript marker and the matching final marker after the remote marker.

- [ ] **Step 4: Add `xdotool` to the immutable M5 profile and GREEN the tests**

Run the focused command again and require zero failures. Add the new test module
to `test_riscv_debian_rootfs_unit` only after the focused suite is green.

### Task 2: Implement bounded guest window and JavaScript evidence

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m6_browser_evidence.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m6_javascript.html`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_session.sh`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_m6_browser.py`

- [ ] **Step 1: Write failing fixture and session tests**

Require the fixture's static `ASTERINAS_JS_PENDING` title, inline script that
sets `ASTERINAS_JS_PASS`, and visible pass token. Require the M4 session to pass
`--enable_javascript=1` only when it accepts the trusted external URL file and
to preserve the quoted local-welcome fallback.

- [ ] **Step 2: Verify RED and implement the fixture/session change**

Run the focused suite, confirm the missing fixture/options failures, make the
minimal changes, and rerun to GREEN.

- [ ] **Step 3: Write failing executable-boundary evidence tests**

Use fake executable `xdotool`, `pgrep`, and a private proc-root. Test:

- one visible NetSurf window with a `百度` or `baidu` title is activated;
- the exact local `file:///usr/share/asterinas/desktop-m6-javascript.html` URL is
  typed without evaluation;
- a changed title reports `limited-pass`;
- an unchanged pending title reports `failed`;
- a NetSurf command line without `--enable_javascript=1` reports `disabled`;
- duplicate windows, oversized titles, command failures, and timeouts emit one
  `DEBIAN_BROWSER_M6_FAIL reason=...` line.

- [ ] **Step 4: Verify RED and implement the bounded evidence script**

The script uses exact positive integer deadlines, bounded external commands,
one selected numeric window ID, a configurable zero-delay test seam, and no
`eval`. The first stable marker precedes a five-second production capture
pause. The JavaScript marker and matching ready marker follow local navigation.

- [ ] **Step 5: Install and order the M6 artifacts**

`configure_desktop_m5_network` installs the script as
`/usr/lib/asterinas/desktop-m6-browser-evidence`, the fixture under
`/usr/share/asterinas`, and a root oneshot service ordered after both
`asterinas-desktop-m5-network.service` and
`asterinas-desktop-m4-evidence.service`. Enable it from `graphical.target`.

### Task 3: Add the two-screenshot M6 QEMU adapter

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m6_browser_gate.py`
- Modify: `tools/riscv/tests/test_debian_m6_browser.py`
- Modify: `Makefile`
- Modify: `tools/riscv/debian/rootfs/README.md`

- [ ] **Step 1: Write failing adapter lifecycle tests**

Use injected serial/monitor fakes to require this order:

```text
M5/M4 ready -> M6 remote marker -> remote screendump
-> one JS status -> matching M6 ready -> JS screendump
```

Require the adapter to keep schema/profile 5, reuse the exact M5 slirp QEMU
argv, invalidate both screenshot names, and publish JavaScript status plus both
metadata dictionaries before `result.json`.

- [ ] **Step 2: Verify RED and implement the thin subclass**

Subclass `DesktopM5QemuOperations`. Let the inherited protocol stop and capture
at the remote marker, then wait for the JS/final markers and call the existing
bounded `capture_rendered_ppm` for the second screenshot. Do not duplicate boot,
artifact, monitor, or cleanup code.

- [ ] **Step 3: Add the explicit Make target and operator documentation**

Add `test_riscv_debian_desktop_m6_browser_gate` with the same nine required
artifact variables as M5, a distinct output variable, SMP=4, and a 300-second
boot timeout. Document that `limited-pass` proves only the local fixture and
that changing Baidu pixels are not compared.

- [ ] **Step 4: Run focused and complete host verification**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m6_browser -v
make test_riscv_debian_rootfs_unit
bash -n tools/riscv/debian/rootfs/desktop_m6_browser_evidence.sh
python3 -m py_compile tools/riscv/debian/rootfs/desktop_m6_browser_gate.py
ruff check tools/riscv/debian/rootfs/desktop_m6_browser_gate.py \
  tools/riscv/tests/test_debian_m6_browser.py
ruff format --check tools/riscv/debian/rootfs/desktop_m6_browser_gate.py \
  tools/riscv/tests/test_debian_m6_browser.py
git diff --check
```

### Task 4: Rebuild and run the Asterinas browser gate

**Files:**
- Generate: `target/debian-riscv/desktop-m5-network/rootfs/*`
- Generate: `target/debian-riscv/desktop-m5-network/m6-qemu-gate/*`

- [ ] **Step 1: Rebuild only the signed M5 rootfs**

Use the pinned `asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached`
image, the existing local proxy, the documented TUNA mirror, and one named
container. Verify three good InRelease signatures, the public rootfs contract,
and the locked RISC-V `xdotool` identity.

- [ ] **Step 2: Run one bounded M6 Asterinas QEMU gate**

Use the already-built Asterinas kernel, U-Boot, four-hart generic-Sv39 DTB, and
M5 stage1 initramfs. Set an outer 480-second timeout. Do not launch a Linux guest
kernel and do not mutate the signed source image.

- [ ] **Step 3: Inspect the complete evidence set**

Require `passed:true`, monotonically ordered M5/M4/M6 markers, one JavaScript
status, matching final marker, exact slirp QEMU argv, complete serial log, two
1280x1024 non-blank PPM files, and no surviving QEMU process.

- [ ] **Step 4: Run final verification, self-review once, commit, and push**

Rerun the complete host/static gate against the final bytes. Perform one concise
self-review without spawning review agents. Commit the implementation, verify a
clean worktree, confirm `origin/main` has not advanced, push `HEAD:main`, and
remove only the exact named disposable container.
