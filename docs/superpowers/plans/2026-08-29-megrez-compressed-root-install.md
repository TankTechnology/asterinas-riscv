# Megrez Compressed Root Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the protected Asterinas-only Megrez Debian installation inside a bounded recovery window by transferring a deterministic gzip representation of the frozen root image and avoiding a redundant 1 GiB readback.

**Architecture:** Keep the signed uncompressed ext2 identity unchanged. Add one host-side atomic streaming gzip publisher, serve that file from the private LAN, and change only the installer data pipeline to decompress before writing the existing protected eMMC target.

**Tech Stack:** Python 3 standard-library `gzip`, POSIX fsync/replace, BusyBox/Debian shell tools in the installer initramfs, `unittest`.

---

### Task 1: Freeze the compressed transport contract

**Files:**
- Modify: `tools/riscv/tests/test_megrez_debian_installer.py`
- Modify: `tools/riscv/tests/test_megrez_install_workflow.py`

- [ ] **Step 1: Write failing tests**

Add tests that require the rendered init script to contain
`sha256sum < "$hash_fifo"` plus
`wget -T 30 -O - '<url>.gz' | gzip -dc | tee "$hash_fifo" | dd`, require two
independently published gzip files to be byte-identical and decompress to the
source, and require `run_network_install` to compress before build/server/serial
while passing the `.gz` path to the server.

- [ ] **Step 2: Run tests and record RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debian_installer \
  tools.riscv.tests.test_megrez_install_workflow -v
```

Expected: failures because the gzip publisher is absent, the script writes
raw HTTP bytes, and the workflow still requires `/debian-root.ext2`.

### Task 2: Implement deterministic gzip publication

**Files:**
- Modify: `tools/riscv/megrez_debian_install.py`
- Test: `tools/riscv/tests/test_megrez_install_workflow.py`

- [ ] **Step 1: Add the minimal publisher**

Implement `_publish_gzip(source, destination)` with streaming reads,
`gzip.GzipFile(filename="", mtime=0, compresslevel=1)`, a same-directory
exclusive temporary file, file fsync, mode 0644, atomic replace, directory
fsync, and temporary cleanup.

- [ ] **Step 2: Integrate before physical effects**

Publish `debian-root.ext2.gz` below the validated transfer directory, build
the installer for the exact URL
`http://10.100.19.216:8080/debian-root.ext2.gz`, and serve the compressed
file's directory. Keep the permit, artifact, and Git validations unchanged.

- [ ] **Step 3: Run focused GREEN**

Run the Task 1 command. Expected: all tests pass.

### Task 3: Decompress in the protected installer

**Files:**
- Modify: `tools/riscv/debian/rootfs/megrez_installer.py`
- Test: `tools/riscv/tests/test_megrez_debian_installer.py`

- [ ] **Step 1: Change only the transport pipeline**

Render the network command as:

```sh
sha256sum < "$hash_fifo" > "$hash_result" &
wget -T 30 -O - 'http://10.100.19.216:8080/debian-root.ext2.gz' \
  | gzip -dc \
  | tee "$hash_fifo" \
  | dd of="$target" bs=1048576 iflag=fullblock conv=notrunc count=1024
wait "$hash_pid"
```

Retain `set -o pipefail`, retry bounds, sync, and the manifest SHA-256
comparison. Parse the FIFO hasher result strictly, require the exact root hash
and `-` stdin name, and require both the foreground pipeline and background
hasher to succeed. Do not perform a second full-device read: the hash covers
the exact decompressed byte stream that `tee` sends to both consumers, not an
independent media readback.

- [ ] **Step 2: Run focused GREEN**

Run the Task 1 command. Expected: all tests pass.

### Task 4: Verify and commit

**Files:**
- Modify: `tools/riscv/debian/rootfs/megrez_installer.py`
- Modify: `tools/riscv/megrez_debian_install.py`
- Modify: `tools/riscv/tests/test_megrez_debian_installer.py`
- Modify: `tools/riscv/tests/test_megrez_install_workflow.py`

- [ ] **Step 1: Run bounded verification**

```bash
python3 -m py_compile \
  tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/megrez_debian_install.py \
  tools/riscv/tests/test_megrez_debian_installer.py \
  tools/riscv/tests/test_megrez_install_workflow.py
ruff check tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/megrez_debian_install.py \
  tools/riscv/tests/test_megrez_debian_installer.py \
  tools/riscv/tests/test_megrez_install_workflow.py
ruff format --check tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/megrez_debian_install.py \
  tools/riscv/tests/test_megrez_debian_installer.py \
  tools/riscv/tests/test_megrez_install_workflow.py
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-08-29-megrez-compressed-root-install-design.md \
  docs/superpowers/plans/2026-08-29-megrez-compressed-root-install.md \
  tools/riscv/debian/rootfs/megrez_installer.py \
  tools/riscv/megrez_debian_install.py \
  tools/riscv/tests/test_megrez_debian_installer.py \
  tools/riscv/tests/test_megrez_install_workflow.py
git commit -m "fix(riscv): compress Megrez Debian installation"
```

### Task 5: Reissue evidence and perform one board attempt

**Files:**
- Generate below: `target/megrez-debian-prewarmed-e81e78eb5/`

- [ ] **Step 1: Recreate the plan and permit at the new Git identity**

Regenerate the schema-2 plan from the same artifact hashes, re-run the bounded
M6 and recovery evidence only if their plan identity changes, and issue a new
preboard permit. Do not reuse a permit whose `git_commit` differs from HEAD.

- [ ] **Step 2: Verify the real compressed artifact**

Require `gzip -t`, record compressed size and SHA-256, decompress through a
streaming SHA-256 check, and require the original root hash
`14f9c496847e9f29c4bdbf414b795c23585d96654c50cc87b8757582fe0bb9c8`.

- [ ] **Step 3: Run one protected install and desktop gate**

Require a fresh U-Boot prompt, use the permit-bound installer, capture
`DEBIAN_INSTALL_PASS` plus a fresh firmware epoch, then boot the exact Asterinas
desktop plan and require the ordered M5/M4/M6 NetSurf Baidu markers. No Linux
runtime and no physical reset are permitted by this plan.
