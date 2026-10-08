# Debian Browser-Ready Wired Network Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Debian desktop browse an external HTTPS resource on both QEMU and the Milk-V Megrez through Asterinas, using the two already reviewed static IPv4 profiles.

**Architecture:** Keep kernel network configuration, VirtIO-Net, and the Megrez DWMAC driver unchanged. Extend the M5 guest evidence service with a strict Megrez profile, update the physical serial gate to require ordered DNS/HTTPS/asset/browser evidence, then rebuild the signed desktop root once and run one QEMU M6 gate plus one physical gate. DHCP, mutable rtnetlink configuration, cable-replug recovery, and Linux as a runtime kernel remain out of scope.

**Tech Stack:** Bash guest evidence, Python 3 gate classifiers, `unittest`, Asterinas RISC-V/Sv39, Debian trixie riscv64, QEMU slirp, Megrez DWMAC/RJ45, NetSurf/Xorg.

---

### Task 1: Freeze the physical guest evidence contract

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] **Step 1: Add failing tests for the complete Megrez marker sequence**

Add a distinct physical tuple while retaining the current public alias:

```python
DESKTOP_M5_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_MEGREZ_DNS resolver=10.2.0.5 fallback=10.2.0.6 host=www.baidu.com",
    "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200",
    "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png",
    "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45",
)
DESKTOP_M5_NETWORK_MILESTONES = DESKTOP_M5_MEGREZ_MILESTONES
```

Test missing, duplicate, and reordered Megrez DNS/HTTPS/asset markers. Preserve the QEMU tuple byte-for-byte.

- [ ] **Step 2: Run the focused RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m5_network -v
```

Expected: the classifier contract fails because the exported physical tuple
still contains only link and READY. The shell lifecycle remains frozen
at that legacy output until Task 2.

- [ ] **Step 3: Implement only the marker/classifier contract**

Make `classify_desktop_m5_network()` consume the new tuple. Do not change QEMU classification or broaden failure matching.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m5_network -v
python3 -m py_compile tools/riscv/debian/rootfs/desktop_m5_network_gate.py
ruff check tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/tests/test_debian_m5_network.py
ruff format --check tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/tests/test_debian_m5_network.py
git diff --check
git add tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/tests/test_debian_m5_network.py
git commit -m "test(riscv): define Megrez browser network evidence"
```

### Task 2: Make the M5 guest evidence environment-aware

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] **Step 1: Add failing shell-lifecycle tests**

Cover these exact cases with the existing fake-command harness:

- the Megrez path requires the exact command-line word
  `asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1`;
- `/etc/resolv.conf` is atomically replaced with primary `10.2.0.5` and
  fallback `10.2.0.6`;
- `getent ahostsv4 www.baidu.com` succeeds;
- HTTPS returns status `200` and local IP `10.100.19.200` without `-k`;
- the Baidu PNG is downloaded as a non-empty bounded temporary file;
- `/run/asterinas-desktop-url` is atomically published;
- wrong bootargs, DNS failure, TLS/HTTP failure, wrong local IP, empty asset,
  and resolver rename failure each emit one stable failure reason;
- the QEMU path still emits its current three exact milestones and keeps
  resolver `10.0.2.3`.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5GuestTests -v
```

Expected: physical DNS, HTTPS, asset, and URL-publication assertions fail.

- [ ] **Step 3: Implement two strict profiles and shared external checks**

Use constants rather than accepting arbitrary environment values:

```bash
readonly MEGREZ_BOOTARG='asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1'
readonly MEGREZ_PRIMARY_DNS='10.2.0.5'
readonly MEGREZ_FALLBACK_DNS='10.2.0.6'
readonly BAIDU_URL='https://www.baidu.com/'
readonly BAIDU_ASSET='https://www.baidu.com/img/flexible/logo/pc/result.png'
```

Select QEMU only by the existing `asterinas.debian_network=qemu-slirp` token.
For the physical branch, split `/proc/cmdline` into words and require an exact
match with `grep -Fx`. Write resolver and URL files through same-directory
temporary files followed by `mv -T`. Run `curl` with certificate validation,
`--fail`, `--location`, and the existing command timeout; never use `-k` or a
host proxy. Download the PNG only to a temporary file, require it to be
non-empty, remove it, and publish only the fixed HTTPS URL.

- [ ] **Step 4: Run GREEN and commit**

```bash
bash -n tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m5_network -v
git diff --check
git add tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh \
  tools/riscv/tests/test_debian_m5_network.py
git commit -m "feat(riscv): prove Megrez DNS and HTTPS"
```

### Task 3: Extend the physical gate through browser evidence

**Files:**
- Modify: `tools/riscv/megrez_gmac_gate.py`
- Modify: `tools/riscv/tests/test_megrez_gmac_gate.py`

- [ ] **Step 1: Add failing ordered-protocol tests**

Require this serial order, which matches the systemd dependencies:

```python
PHYSICAL_MILESTONES = (
    b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 ",
    *(marker.encode() for marker in DESKTOP_M5_MEGREZ_MILESTONES),
    b"DEBIAN_DESKTOP_M4_READY user=asterinas display=:0",
    b"DEBIAN_BROWSER_M6_REMOTE host=www.baidu.com resource=logo-png foreground=active",
)
```

The terminal marker is dynamic and must be exactly one of:

```text
DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass
DEBIAN_BROWSER_M6_READY remote=baidu javascript=disabled
DEBIAN_BROWSER_M6_READY remote=baidu javascript=failed
```

Test split reads, missing/duplicate/reordered physical markers, duplicate or
mismatched M6 status, `DEBIAN_BROWSER_M6_FAIL`, and a fatal marker received
during the final drain. Do not use ICMP as a proxy for browser traffic.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_gmac_gate -v
```

Expected: browser-complete transcripts are rejected because the current gate
stops at the old M5 READY marker and expects M4 before M5.

- [ ] **Step 3: Implement the strict physical classifier**

Import `DESKTOP_M5_MEGREZ_MILESTONES` and the M6 allowed JavaScript statuses.
Wait for the M6 READY prefix, drain the remaining serial output, then classify
the complete transcript. Add `DEBIAN_BROWSER_M6_FAIL` to fatal markers. Record
the accepted JavaScript status in `result.json`; keep the address-conflict
precheck, serial cleanup, no-`saveenv` policy, and static bootarg unchanged.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_gmac_gate -v
python3 -m py_compile tools/riscv/megrez_gmac_gate.py
ruff check tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
ruff format --check tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git diff --check
git add tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git commit -m "test(riscv): gate Megrez browser networking"
```

### Task 4: Document the bounded operator contract and run host gates

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`

- [ ] **Step 1: Add the physical browser-ready section**

Document the exact static profile, resolvers, expected marker order, TFTP
transport, M6 JavaScript boundary, and evidence paths. State explicitly that
this milestone does not cover DHCP, `RTM_NEWADDR`, `RTM_NEWROUTE`, cable
replug, GMAC failover, Wi-Fi, USB Ethernet, Firefox, or modern JavaScript.

- [ ] **Step 2: Run the complete bounded host gate once**

```bash
make test_riscv_debian_rootfs_unit
make test_riscv_megrez_gmac_unit
bash -n tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh
python3 -m py_compile \
  tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_debian_m5_network.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
ruff check tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_debian_m5_network.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
ruff format --check tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_debian_m5_network.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git diff --check
```

Expected: all host tests and static checks pass. Do not start Docker or QEMU
as part of this host-only task.

- [ ] **Step 3: Commit the operator documentation**

```bash
git add tools/riscv/debian/rootfs/README.md
git commit -m "docs(riscv): describe browser-ready wired network"
```

### Task 5: Rebuild once and run one QEMU browser gate

**Files:**
- Generate only: `target/debian-riscv/desktop-m5-network/`
- Generate only: `target/osdk/aster-kernel/aster-kernel-osdk-bin.Image`
- Generate only: `target/qemu-uboot/debian-root/qemu-virt.dtb`

- [ ] **Step 1: Build the signed root and Stage1 once**

Use the pinned Asterinas container, the existing content-addressed Debian
package cache, and the working Clash proxy or a verified Chinese Debian mirror.
Do not repeat a successful rootfs build.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-m5-network
tools/riscv/debian/rootfs/build_stage1.sh \
  target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/desktop-m5-network/rootfs/packages.lock
```

Expected: the root contract exits quietly with status 0 and the Stage1 archive
contains only `.` and `init`.

- [ ] **Step 2: Build the current Sv39 SMP=4 Asterinas kernel once**

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Use the existing rustup/toolchain cache and local proxy; inspect progress
rather than waiting on a duplicate download. Expected Image:
`target/osdk/aster-kernel/aster-kernel-osdk-bin.Image`.

- [ ] **Step 3: Run only the M6 QEMU gate**

M6 subsumes the M5 DNS/HTTPS gate, so do not run both.

```bash
make test_riscv_debian_desktop_m6_browser_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/desktop-m5-network/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/desktop-m5-network/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M6_BROWSER_GATE_OUTPUT="$PWD/target/debian-riscv/desktop-m5-network/m6-qemu-gate"
```

Expected: `result.json` has `passed: true`; serial contains QEMU DNS, HTTPS,
M4 READY, M6 remote, and M6 READY markers; both screenshots are non-blank;
the complete serial log contains no panic, oops, M5 failure, or M6 failure.

- [ ] **Step 4: Fix only a reproduced source defect**

If the real gate finds a defect, add one focused failing test, implement the
smallest fix, rerun that test and the M6 gate, and commit a narrow
`fix(riscv): ...`. Do not start a general network refactor.

### Task 6: Install and pass one physical Megrez browser gate

**Files:**
- Generate only: `target/megrez-browser-network/`
- Create after PASS: `docs/porting/evidence/2026-08-27-megrez-browser-ready-network.md`

- [ ] **Step 1: Freeze and verify the physical inputs**

Use the already captured physical DTB and frozen GMAC contract:

```bash
mkdir -p target/megrez-browser-network/tftp
cp --reflink=auto \
  target/megrez-board-runs/megrez-usb-keyboard-043dde9ee-20260722T120731Z/evidence/eic7700-milkv-megrez-linux-working.dtb \
  target/megrez-browser-network/tftp/eic7700-milkv-megrez.dtb
python3 -m tools.riscv.megrez_gmac_contract verify \
  --dtb target/megrez-browser-network/tftp/eic7700-milkv-megrez.dtb \
  --firmware-mac0 00:48:54:71:00:47 \
  --firmware-mac1 00:48:54:71:00:48 \
  --contract tools/riscv/megrez_gmac_contract.v1.json
cp --reflink=auto target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  target/megrez-browser-network/tftp/asterinas-browser-net.booti
cp --reflink=auto \
  target/debian-riscv/desktop-m5-network/stage1/initramfs.cpio \
  target/megrez-browser-network/tftp/debian-browser-stage1.cpio
```

Expected: DTB size 154800 and CRC32 `4afcb20e`; all three files are non-empty
regular files. Record SHA-256 and U-Boot CRC32 for each before opening serial.

- [ ] **Step 2: Install the new signed root through Asterinas**

Build the restart-safe installer from the new M5 root and the existing verified
BusyBox base archive:

```bash
python3 -m tools.riscv.debian.rootfs.megrez_installer \
  --base-cpio target/megrez-board-runs/megrez-full-initramfs-712208ba4-20260721T155000Z/artifacts/initramfs-full-712208ba4.cpio \
  --root-image target/debian-riscv/desktop-m5-network/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/desktop-m5-network/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/desktop-m5-network/rootfs/packages.lock \
  --output target/megrez-browser-network/tftp/debian-browser-installer.cpio
```

Serve only `target/megrez-browser-network/tftp` on host address
`10.100.19.216` with this foreground server in a dedicated terminal:

```bash
sudo dnsmasq --no-daemon --port=0 --bind-interfaces \
  --interface=enp12s0 --listen-address=10.100.19.216 \
  --enable-tftp --tftp-root="$PWD/target/megrez-browser-network/tftp" \
  --pid-file="$PWD/target/megrez-browser-network/dnsmasq.pid"
```

Compute exact CRC32 values and boot the installer through Asterinas:

```bash
crc32_file() {
  python3 -c 'import pathlib,sys,zlib; print(f"{zlib.crc32(pathlib.Path(sys.argv[1]).read_bytes()):08x}")' "$1"
}
BOOTI_CRC32="$(crc32_file target/megrez-browser-network/tftp/asterinas-browser-net.booti)"
INSTALLER_CRC32="$(crc32_file target/megrez-browser-network/tftp/debian-browser-installer.cpio)"
python3 tools/riscv/megrez_board_session.py /dev/ttyUSB0 \
  --booti asterinas-browser-net.booti \
  --dtb eic7700-milkv-megrez.dtb \
  --initrd debian-browser-installer.cpio \
  --expected-crc32 "booti=$BOOTI_CRC32,dtb=4afcb20e,initrd=$INSTALLER_CRC32" \
  --load-transport tftp \
  --tftp-board-address 10.100.19.200 \
  --tftp-server-address 10.100.19.216 \
  --tftp-netmask 255.255.248.0 \
  --bootargs 'console=tty0 console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.mmc_write_partition2 asterinas.reboot_after=1800' \
  --final-profile installer --milestone-timeout 1800 --yes \
  --log target/megrez-browser-network/installer.serial.log
```

Require `DEBIAN_INSTALL_PASS` with the exact new root SHA-256 before continuing.
Linux may transfer the immutable files over LAN, but Asterinas must perform
the partition-2 write and readback verification.

- [ ] **Step 3: Preflight the live browser gate**

Within one minute, report the actual serial state. Verify `/dev/ttyUSB0`, host
`enp12s0` address `10.100.19.216/21`, link carrier, direct DNS to `10.2.0.5`,
and that `10.100.19.200` is unused. If U-Boot is not available, request one
manual reset immediately; do not wait for a long protection timer.

- [ ] **Step 4: Run the physical gate once over TFTP**

Compute the two remaining CRC32 values with the same `crc32_file` function,
then run:

```bash
BOOTI_CRC32="$(crc32_file target/megrez-browser-network/tftp/asterinas-browser-net.booti)"
STAGE1_CRC32="$(crc32_file target/megrez-browser-network/tftp/debian-browser-stage1.cpio)"
python3 -m tools.riscv.megrez_gmac_gate /dev/ttyUSB0 \
  --booti asterinas-browser-net.booti \
  --dtb eic7700-milkv-megrez.dtb \
  --initrd debian-browser-stage1.cpio \
  --expected-crc32 "booti=$BOOTI_CRC32,dtb=4afcb20e,initrd=$STAGE1_CRC32" \
  --host-interface enp12s0 \
  --load-transport tftp \
  --tftp-board-address 10.100.19.200 \
  --tftp-server-address 10.100.19.216 \
  --tftp-netmask 255.255.248.0 \
  --output-directory target/megrez-browser-network/gate \
  --boot-timeout 300 --drain-timeout 5
```

Expected: selected GMAC, physical M5 DNS/HTTPS/asset READY, M4 READY, M6
remote and READY, `passed: true`, and no panic/oops/DMA/M5/M6 failure in the
fully drained transcript. UDP DNS and TCP/TLS are the network proof; ICMP is
outside this browser-ready contract.

- [ ] **Step 5: Leave one persistent desktop and record evidence**

After the bounded gate passes, repeat the same frozen boot without an automatic
reboot timer, stop only the host serial reader after M6 READY, and leave HDMI,
keyboard, mouse, terminal, file manager, NetSurf, and RJ45 running. Record the
source commit and all artifact/log hashes in
`docs/porting/evidence/2026-08-27-megrez-browser-ready-network.md`.

- [ ] **Step 6: Verify and commit the evidence page**

```bash
python3 -m json.tool target/megrez-browser-network/gate/result.json
test "$(python3 -c 'import json; print(json.load(open("target/megrez-browser-network/gate/result.json"))["passed"])')" = True
git diff --check
git add docs/porting/evidence/2026-08-27-megrez-browser-ready-network.md
git commit -m "docs(riscv): record Megrez browser networking"
```

## Completion gate

This milestone is complete only when the worktree is clean and fresh evidence
shows: both host unit targets pass; QEMU M6 passes at SMP=4; the physical root
was installed and read back by Asterinas; Megrez proves DNS, validated HTTPS,
PNG transfer, NetSurf foreground evidence, and bidirectional ICMP; and the
complete serial transcript has no fatal marker. Remote CI monitoring, cable
replug, long stability soaking, DHCP, Firefox, and broader desktop polish are
not completion requirements.
