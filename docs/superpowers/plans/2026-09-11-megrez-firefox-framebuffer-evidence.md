# Megrez Firefox framebuffer evidence implementation plan

> **Execution note:** Implement this plan inline in the current task with the
> `superpowers:executing-plans` workflow. Do not delegate it to subagents.

**Goal:** Replace only the physical homepage transaction's intermittent Firefox screenshot request with a hash-bound direct framebuffer PNG.

**Architecture:** Keep Marionette as the authoritative URL/TLS/DOM observer, close its transport after the snapshot, and then read `/dev/fb0` through the existing Linux-compatible framebuffer ABI. Encode the current BGR-reserved scanout as a bounded nonblank PNG and bind its identity into the final marker and host result.

**Tech Stack:** Python 3 standard library, Linux framebuffer ioctls, `unittest`, persistent Asterinas development container, existing MMC-only Megrez runner.

---

### Task 1: Specify the framebuffer PNG boundary

**Files:**

- Modify: `tools/riscv/tests/test_debian_browser_web.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_marionette_gate.py`

- [ ] Add failing tests that convert deterministic BGR-reserved rows with
  padding, reject a uniform scanout, reject unsupported framebuffer metadata,
  and produce a structurally valid RGB PNG.
- [ ] Run the focused test and verify it fails because the framebuffer capture
  API is absent.
- [ ] Add strict framebuffer ioctl parsing, exact-read logic, sampled-pixel
  diversity validation, and bounded PNG encoding using `struct`, `fcntl`,
  `hashlib`, and `zlib`.
- [ ] Re-run the focused tests and verify they pass.

### Task 2: Separate DOM observation from display capture

**Files:**

- Modify: `tools/riscv/tests/test_debian_browser_web.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_marionette_gate.py`

- [ ] Add failing tests for `--screenshot-backend marionette` compatibility,
  framebuffer capture only after `client.close()`, rejection of framebuffer
  mode for the full suite, and a final marker containing source, dimensions,
  and PNG SHA-256.
- [ ] Run those tests and verify the missing backend behavior is the failure.
- [ ] Thread the backend through the lightweight gate, retain the current
  Marionette default, capture `/dev/fb0` only after transport close, and emit
  the final marker only after the PNG exists and passes the local contract.
- [ ] Re-run all `BrowserWebContractTests`.

### Task 3: Bind the physical host result to framebuffer evidence

**Files:**

- Modify: `tools/riscv/tests/test_megrez_firefox_browse.py`
- Modify: `tools/riscv/megrez_firefox_browse.py`

- [ ] Add failing tests requiring the physical guest command to select the
  framebuffer backend and requiring marker source/hash/dimensions to match the
  transferred PNG.
- [ ] Run the focused tests and verify the old command and marker contract fail.
- [ ] Add the backend flag to the physical command and extend host validation
  without changing the full browser-web gate or publication filenames.
- [ ] Re-run Firefox browse and boot-stability unit tests.

### Task 4: Package and verify the Stage1 delta

**Files:**

- Modify: `tools/riscv/tests/test_debian_rootfs.py` only if its exact Stage1
  identity assertions require the enlarged existing browser tool.
- Generated under ignored `target/`: a new Stage1 CPIO and physical plan.

- [ ] Run `sh -n`, Python bytecode compilation, formatting, and diff checks.
- [ ] Run the affected browser, physical graphics, boot stability, and rootfs
  tests through `tools/docker/run_dev_container.sh` with persistent caches.
- [ ] Build only Stage1; verify its entries, mode, size, SHA-256, and CRC32.
- [ ] Boot RockOS once, copy only the new Stage1 file to MMC partition 1,
  unmount it, and record an attestation.  Do not modify partition 2 or replace
  the kernel/DTB.

### Task 5: Run bounded physical acceptance

**Files:**

- Generated under ignored `target/megrez-desktop/`: private evidence bundles.

- [ ] Run one unattended physical homepage transaction and retain serial,
  diagnostics, JSON, PNG, proxy, result, and checksum evidence.
- [ ] Verify strict Baidu URL/TLS/DOM identity, `screenshot_source=framebuffer`,
  1920-by-1080 PNG identity, nonblank pixels, stable Firefox PID with zero
  restarts, MMC-only artifacts, and recovery to a fresh U-Boot prompt.
- [ ] Inspect the PNG visually and run `sha256sum -c` inside the evidence
  directory.
- [ ] Run a second unchanged transaction.  Only two complete passes justify a
  repeatability claim; any difference returns the work to evidence analysis.
- [ ] Review the diff normally, commit the focused change and evidence notes,
  and leave remote `main` untouched until the user-approved integration step.
