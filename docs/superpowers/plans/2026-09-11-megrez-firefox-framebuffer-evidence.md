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

- [x] Add failing tests that convert deterministic BGR-reserved rows with
  padding, reject a uniform scanout, reject unsupported framebuffer metadata,
  and produce a structurally valid RGB PNG.
- [x] Run the focused test and verify it fails because the framebuffer capture
  API is absent.
- [x] Add strict framebuffer ioctl parsing, exact-read logic, sampled-pixel
  diversity validation, and bounded PNG encoding using `struct`, `fcntl`,
  `hashlib`, and `zlib`.
- [x] Re-run the focused tests and verify they pass.

### Task 2: Separate DOM observation from display capture

**Files:**

- Modify: `tools/riscv/tests/test_debian_browser_web.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_marionette_gate.py`

- [x] Add failing tests for `--screenshot-backend marionette` compatibility,
  framebuffer capture only after `client.close()`, rejection of framebuffer
  mode for the full suite, and a final marker containing source, dimensions,
  and PNG SHA-256.
- [x] Run those tests and verify the missing backend behavior is the failure.
- [x] Thread the backend through the lightweight gate, retain the current
  Marionette default, capture `/dev/fb0` only after transport close, and emit
  the final marker only after the PNG exists and passes the local contract.
- [x] Re-run all `BrowserWebContractTests`.

### Task 3: Bind the physical host result to framebuffer evidence

**Files:**

- Modify: `tools/riscv/tests/test_megrez_firefox_browse.py`
- Modify: `tools/riscv/megrez_firefox_browse.py`

- [x] Add failing tests requiring the physical guest command to select the
  framebuffer backend and requiring marker source/hash/dimensions to match the
  transferred PNG.
- [x] Run the focused tests and verify the old command and marker contract fail.
- [x] Add the backend flag to the physical command and extend host validation
  without changing the full browser-web gate or publication filenames.
- [x] Re-run Firefox browse and boot-stability unit tests.

### Task 4: Package and verify the Stage1 delta

**Files:**

- Modify: `tools/riscv/tests/test_debian_rootfs.py` only if its exact Stage1
  identity assertions require the enlarged existing browser tool.
- Generated under ignored `target/`: a new Stage1 CPIO and physical plan.

- [x] Run Bash syntax, Python bytecode, formatting, and diff checks.  The
  persistent image has no `ruff`, so no package was downloaded; the Python
  sources were inspected manually and `git diff --check` passed.
- [x] Run the affected browser, physical graphics, boot stability, and rootfs
  tests through `tools/docker/run_dev_container.sh` with persistent caches.
- [x] Build only Stage1; verify its entries, mode, size, SHA-256, and CRC32.
- [x] Boot RockOS once, copy only the new Stage1 file to MMC partition 1,
  unmount it, and record an attestation.  Do not modify partition 2 or replace
  the kernel/DTB.

Stage1 was published as `asterinas-6b3037dc-949149fc-stage1.cpio`: 658944
bytes, SHA-256
`949149fcbec4b28869c1bbd5367314e3f8fe92f5a742abf200be8d9175f9abed`,
CRC32 `306f3e18`.  RockOS attested that Stage1 plus the unchanged kernel and DTB
matched plan
`9232c6d29bbeecc48c6001396986c92b3ddecfcbe471c2b9c62f8222dfb1982c`.

### Task 5: Run bounded physical acceptance

**Files:**

- Generated under ignored `target/megrez-desktop/`: private evidence bundles.

- [x] Run one unattended physical homepage transaction and retain serial,
  diagnostics, JSON, PNG, proxy, result, and checksum evidence.
- [x] Verify strict Baidu URL/TLS/DOM identity, `screenshot_source=framebuffer`,
  1920-by-1080 PNG identity, nonblank pixels, stable Firefox PID with zero
  restarts, MMC-only artifacts, and recovery to a fresh U-Boot prompt.
- [x] Inspect the PNG visually and run `sha256sum -c` inside the evidence
  directory.
- [x] Run a second unchanged transaction.  Only two complete passes justify a
  repeatability claim; any difference returns the work to evidence analysis.
- [x] Review the diff normally, commit the focused change and evidence notes,
  and leave remote `main` untouched until the user-approved integration step.

The first boundary attempt reached a ready Firefox/Xorg/Openbox/framebuffer but
failed before navigation because both external HTTP clock probes timed out.
Commit `a48e2567b` removed that unrelated network dependency: the host now sets
UTC over the existing serial control channel and the guest emits bounded,
cross-checked host/guest epoch evidence.

Two subsequent unchanged transactions passed and recovered to U-Boot:

- `browse-5e17d6dfbded1e7f`: 806.876 seconds, Firefox PID 115, screenshot
  SHA-256
  `7e80c420e4a7ffcdff91cff9ef7a5d19755bc4d9ce5f5da902472397787a196e`.
- `browse-1c0e99c019cdc1db`: 870.703 seconds, Firefox PID 114, screenshot
  SHA-256
  `5c2e8007601446fdb853b30af2b3074b1cdcda7f165e0a8f0266a702d0f3278b`.

Both PNGs are 1920 by 1080, visibly show the complete Firefox Baidu homepage,
and have about 60.8 percent sampled nonblack coverage.  Every file in both
evidence bundles passed its recorded checksum.  The final persistent-container
regression run passed all 332 selected tests in 22.378 seconds.  Normal review
found no correctness, boundary-validation, resource-lifetime, or hardware-ABI
issue.  Remote `main` remains untouched.
