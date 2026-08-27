# Debian M5 QEMU Internet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that the Debian M5 desktop reaches `https://www.baidu.com/` through Asterinas and QEMU slirp, then hands that URL to NetSurf. Remote-page rendering and JavaScript remain separate follow-up evidence rather than network-gate claims.

**Architecture:** Keep the existing physical-GMAC evidence contract intact and add a QEMU-specific branch selected by the exact `asterinas.debian_network=qemu-slirp` kernel token. The guest writes the QEMU slirp resolver, proves DNS and HTTPS in order, then publishes a bounded browser URL for the existing unprivileged desktop session. A dedicated M5 QEMU adapter adds only the VirtIO NIC to the existing graphical gate and classifies the complete serial transcript plus framebuffer evidence. Linux ICMP and rtnetlink compatibility are not used as proxies for the working UDP/TCP/TLS data path.

**Tech Stack:** Python `unittest`, Bash/systemd guest probes, Debian RISC-V `curl`/NetSurf, QEMU RISC-V `virt` with slirp and VirtIO-net.

---

### Task 1: Reproduce the current external-network boundary

**Files:**
- Inspect: `target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2`
- Inspect: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Inspect: `tools/riscv/debian/rootfs/desktop_m4_session.sh`

- [x] **Step 1: Confirm current immutable inputs**

Run `debugfs` against the M5 image and record `/etc/resolv.conf`, the presence of `getent`, `openssl`, `netsurf-gtk`, and the absence of the `curl` CLI.

- [x] **Step 2: Run one disposable QEMU diagnostic**

Use an independent root copy. Point the copy at slirp DNS `10.0.2.3`, launch NetSurf directly at `https://www.baidu.com/`, and record ordered gateway, DNS, TLS/HTTP, NetSurf, serial, and PPM evidence. Do not edit the signed source image.

- [x] **Step 3: State one root-cause hypothesis**

Identify the first failing boundary only. Do not change production code until the transcript distinguishes route, DNS, TLS/HTTP, browser fetch, and browser render.

### Task 2: Freeze the dual physical/QEMU network evidence contract

**Files:**
- Modify: `tools/riscv/tests/test_debian_m5_network.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`
- Modify: `tools/riscv/debian/rootfs/profiles.py`

- [x] **Step 1: Write failing profile and classifier tests**

Require `curl` in the M5 package identity. Preserve the existing physical markers and add ordered QEMU markers for slirp DNS `10.0.2.3`, `www.baidu.com` HTTPS success from local address `10.0.2.15`, and final ready state.

- [x] **Step 2: Verify RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest tools.riscv.tests.test_debian_m5_network -v
```

Expected: failures because the profile lacks `curl` and the QEMU classifier contract does not exist.

- [x] **Step 3: Implement the immutable contract**

Add `curl` to `requested_packages` and `identity_packages`. Export separate physical and QEMU milestone tuples and require strict ordered classification.

- [x] **Step 4: Verify GREEN**

Run the same focused test command and require zero failures.

### Task 3: Make guest DNS and browser launch platform-aware

**Files:**
- Modify: `tools/riscv/tests/test_debian_m5_network.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_session.sh`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`

- [x] **Step 1: Write failing QEMU evidence tests**

Fake `ip`, `ping`, `getent`, and `curl` as real executable boundaries. Require the exact kernel token to select QEMU, write `nameserver 10.0.2.3`, resolve `www.baidu.com`, require HTTPS 200/3xx from local address `10.0.2.15`, never call the incomplete `ip`/`ping` interfaces, and atomically write `https://www.baidu.com/` to `/run/asterinas-desktop-url`.

- [x] **Step 2: Verify RED**

Run the focused M5 test and confirm that the current physical-only script rejects the QEMU address.

- [x] **Step 3: Implement the QEMU branch without weakening browser evidence**

Keep the current physical static address behavior. Add only the exact QEMU
branch, bounded commands, resolver update, and runtime URL publication. The
later physical gate directly tests DNS and HTTPS instead of relying on ICMP.

- [x] **Step 4: Write and run a failing desktop-session test**

Require `desktop_m4_session.sh` to read one bounded `http://` or `https://` URL from `/run/asterinas-desktop-url`, falling back to the local welcome page for missing or invalid contents.

- [x] **Step 5: Implement the minimal browser URL selection**

Pass the selected URL as one quoted argument to `netsurf-gtk`; do not evaluate configuration text.

- [x] **Step 6: Order the network service before the desktop session**

Change the M5 unit to run after local filesystems and before `asterinas-desktop-m4.service`, while leaving a network failure non-fatal to local desktop startup.

### Task 4: Add the QEMU M5 internet adapter

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`
- Modify: `Makefile`
- Modify: `tools/riscv/debian/rootfs/README.md`

- [x] **Step 1: Write failing adapter tests**

Require the existing generic-Sv39 SMP4 graphical device set plus exactly `-netdev user,id=net0` and `-device virtio-net-device,netdev=net0`, with no proxy or TAP dependency. Require schema/profile 5 and the ordered QEMU network plus M4 desktop milestones.

- [x] **Step 2: Verify RED**

Run the focused test and confirm import/API failures for the absent adapter.

- [x] **Step 3: Implement the thin adapter**

Subclass `DesktopM4Operations`, override only profile identity, milestone classification, artifact prefix, and the QEMU argv constructor. Reuse the existing bounded lifecycle, screendump, pinned artifact handling, and process-group cleanup.

- [x] **Step 4: Add a Make target and operator command**

Expose an explicit `test_riscv_debian_desktop_m5_qemu_gate` target using the same artifact variables as the existing Debian gates.

### Task 5: Rebuild and prove the experience

**Files:**
- Generate: `target/debian-riscv/desktop-m5-network/rootfs/*`
- Generate: a private QEMU gate output directory

- [x] **Step 1: Run full host verification**

Run the Debian rootfs unit target, Bash syntax checks, Python compilation, Ruff check/format check, and `git diff --check`.

- [x] **Step 2: Rebuild only the M5 signed rootfs**

Use the pinned Asterinas container, existing local proxy, and the documented TUNA mirror. Verify the frozen manifest and the new `curl` identity.

- [x] **Step 3: Run the QEMU M5 gate**

Require ordered DNS/HTTPS/desktop markers, a final passing result, a complete serial log, and a non-blank desktop PPM screenshot. Do not call that screenshot proof that Baidu rendered.

- [ ] **Step 4: Run a separate browser-render and local JavaScript smoke**

First make the captured foreground window prove NetSurf received the Baidu URL, then use a local HTML page whose script changes a unique DOM token. Report JavaScript as `disabled`, `limited-pass`, or `failed`; never use this result to downgrade a successful network gate.

- [ ] **Step 5: Commit and push**

Commit the focused source changes, verify the exact committed tree, then push the branch to `asterinas-riscv/main`. Do not merge remote PRs.
