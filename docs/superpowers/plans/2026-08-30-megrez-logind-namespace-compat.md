# Megrez Debian logind Namespace Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start Debian systemd-logind and the existing desktop/NetSurf stack on Megrez without invoking namespace facilities that Asterinas does not yet implement.

**Architecture:** Add one exact systemd drop-in during desktop-root generation. Keep the kernel, Stage1 handoff, signed-package contract, desktop services, and browser evidence unchanged; prove the new root in QEMU before installing that exact image through Asterinas on Megrez.

**Tech Stack:** Bash rootfs builder, Python unittest, systemd 257 unit configuration, ext2/debugfs, Asterinas RISC-V QEMU and protected Megrez workflow.

---

### Task 1: Generate the exact logind compatibility drop-in

**Files:**
- Modify: `tools/riscv/tests/test_debian_signed_sources.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`

- [ ] **Step 1: Write the failing rootfs-generation test**

Add a test that sources `build_rootfs.sh`, calls a new
`configure_logind_namespace_compatibility "$stage"`, and requires mode `0644`
with these exact bytes:

```ini
[Service]
# Asterinas does not yet provide the user/mount namespace contract used by
# Debian's systemd-logind sandbox. Keep functional logind without that sandbox.
PrivateTmp=no
ProtectControlGroups=no
ProtectHome=no
ProtectKernelLogs=no
ProtectKernelModules=no
ProtectSystem=no
ReadWritePaths=
```

- [ ] **Step 2: Run the focused test and record RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_signed_sources.DebianSignedSourcesTests.test_desktop_profiles_disable_logind_mount_namespace -v
```

Expected: nonzero because `configure_logind_namespace_compatibility` is not
defined and no drop-in exists.

- [ ] **Step 3: Add the minimal builder helper and call it for desktops**

Add this helper immediately before `configure_desktop()`:

```bash
configure_logind_namespace_compatibility() {
    local stage="$1"
    local directory="$stage/etc/systemd/system/systemd-logind.service.d"
    local output="$directory/asterinas-namespace-compat.conf"

    install -d -m 0755 -- "$directory"
    cat >"$output" <<'EOF'
[Service]
# Asterinas does not yet provide the user/mount namespace contract used by
# Debian's systemd-logind sandbox. Keep functional logind without that sandbox.
PrivateTmp=no
ProtectControlGroups=no
ProtectHome=no
ProtectKernelLogs=no
ProtectKernelModules=no
ProtectSystem=no
ReadWritePaths=
EOF
    chmod 0644 -- "$output"
}
```

Call it once near the start of `configure_desktop()`, so every desktop profile
gets the same compatibility boundary. Remove the old browser-m5 timeout-only
override because it preserves the incompatible namespace setup.

- [ ] **Step 4: Run focused and rootfs host tests**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_signed_sources.DebianSignedSourcesTests.test_desktop_profiles_disable_logind_mount_namespace -v
make test_riscv_debian_rootfs_unit
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
python3 -m ruff check tools/riscv/tests/test_debian_signed_sources.py
python3 -m ruff format --check tools/riscv/tests/test_debian_signed_sources.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit the compatibility change**

```bash
git add tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/tests/test_debian_signed_sources.py
git commit -m "fix(riscv): start logind without namespaces"
```

### Task 2: Build and prove a fresh immutable desktop root

**Files:**
- Generated only: `target/megrez-logind-compat/**`

- [ ] **Step 1: Build the signed desktop root once**

Use the already prepared pinned Asterinas container and content-addressed
cache:

```bash
docker exec -w /root/asterinas codex-debian-m5-batch-build-98fc05fb2 \
  bash -lc 'SOURCE_DATE_EPOCH=1704067200 \
    tools/riscv/debian/rootfs/build_rootfs.sh \
      --profile desktop-m5-network \
      --output-dir target/megrez-logind-compat/rootfs \
      --cache-dir target/debian-riscv/cache'
```

Expected: phases 1 through 8 succeed and publish a new ext2 image, manifest,
lock, InRelease, and checksums.

- [ ] **Step 2: Validate identity and embedded configuration**

Run:

```bash
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/megrez-logind-compat/rootfs/debian-root.ext2 \
  --manifest target/megrez-logind-compat/rootfs/rootfs-manifest.json \
  --packages-lock target/megrez-logind-compat/rootfs/packages.lock
debugfs -R 'stat /etc/systemd/system/systemd-logind.service.d/asterinas-namespace-compat.conf' \
  target/megrez-logind-compat/rootfs/debian-root.ext2
debugfs -R 'cat /etc/systemd/system/systemd-logind.service.d/asterinas-namespace-compat.conf' \
  target/megrez-logind-compat/rootfs/debian-root.ext2
```

Require the new image identity, mode `0644`, exact drop-in bytes, and no
`asterinas-browser-m5-timeout.conf`.

- [ ] **Step 3: Run the existing M6 QEMU gate**

Create a schema-2 300-second plan using the new root and current committed
kernel/Stage1/DTBs/U-Boot, then run:

```bash
PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_debug simulate \
  --tier desktop \
  --output-directory target/megrez-logind-compat/desktop-simulation \
  target/megrez-logind-compat/plan.json
```

Expected: ordered network, udev, logind, input, Xorg, desktop, remote Baidu
asset, JavaScript-status, and browser-ready markers, with no `(sd-mkuserns)`,
syscall 272, syscall 428, or `CLONE_NEWUSER` retry storm.

### Task 3: Install and verify the exact root on Megrez

**Files:**
- Generated only: `target/megrez-logind-compat/board-*/**`

- [ ] **Step 1: Bind recovery and preboard evidence**

Create plan-bound recovery evidence from the already validated software reboot
path and issue a clean-commit preboard permit for the new plan.

- [ ] **Step 2: Install through Asterinas**

Use the existing bounded LAN/MMC installer to write the exact validated ext2
image to partition 2. Require its final SHA-256 marker to equal the plan's
`root_image` SHA-256; do not use Linux as a boot or installation bypass.

- [ ] **Step 3: Run one protected physical M6 boot**

Start the permit-bound board gate with a cached kernel transfer and a 300-second
software reboot. Success requires the full physical marker sequence ending in:

```text
DEBIAN_BROWSER_M6_REMOTE host=www.baidu.com resource=logo-png foreground=active
DEBIAN_BROWSER_M6_JAVASCRIPT status=<recorded-status>
DEBIAN_BROWSER_M6_READY remote=baidu javascript=<same-status>
```

The board must remain interactive through HDMI/USB and return to U-Boot through
software recovery; no manual reset is part of the pass condition.

- [ ] **Step 4: Record evidence and push the milestone**

Add only source/docs evidence appropriate for the repository, run the focused
host/static checks once, commit the evidence, and push the resulting commits to
the `asterinas-riscv` main branch after confirming its fast-forward state.
