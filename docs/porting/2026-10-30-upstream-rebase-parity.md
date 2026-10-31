# Upstream rebase: current state

Base: official `upstream/main` at `cdd5bc109` (2026-10-30).
Working branch: `rebase/upstream-20261029`.
Nothing is pushed to `upstream`. The integration branch is backed up at
`origin/rebase/upstream-20261029`; the QEMU net/procfs work is committed on a
short-lived local feature branch before being folded into the integration line.

The repository now carries only the QEMU-supported Debian surface:

- the upstream RISC-V kernel and OSDK;
- two Debian rootfs profiles (`minimal`, `browser-web`);
- the full Stage1 handoff and the lightweight direct-boot checkpoint;
- the direct QEMU gate;
- the current fbdev desktop, network evidence, devpts/PTY support, and browser
  runtime.

The paused PowerVR/DRM line, Megrez board deployment, physical-graphics gates,
M2-M4 milestone gates, old host gate modules, and one-off diagnostics were
removed from this line. Their history remains in the old product tree at
`/mnt/shared/xaj/Program/asterinas/.claude/worktrees/maintainability-phase0`.

## Verified on the rebased line

| Area | State |
|---|---|
| Build | Upstream RISC-V kernel builds through OSDK 0.19 with `--scheme riscv --boot-method vmm-direct`; a raw Linux Image sidecar is emitted. |
| Debian rootfs | `minimal` and `browser-web` build from signed Debian Trixie indexes and pass the rootfs contract. The browser root includes the fixed Firefox trust contract. |
| Direct QEMU boot | The direct gate boots the kernel with an existing rootfs as `/dev/vda` and checks gcc (real compile/run), Python, Node, and git. An optional dsh image is checked through the same path. |
| Stage1 | The full initramfs compiles, verifies its entry list, reaches systemd, and reaches `multi-user.target`; the browser handoff publishes `/dev/ptmx`, mounts devpts, and provides `/dev/shm`. |
| Kernel logging | `/dev/kmsg`, a bounded kernel log ring, `SO_TIMESTAMP`, `/proc/sys/kernel/random/boot_id`, and `syslog(2)`/`dmesg` are present; systemd-journald is active. |
| Networking | virtio-net appears as `eth0`; the slirp fixture, DNS, direct HTTPS, and the host proxy path work. `RTM_NEWLINK`, `RTM_SETLINK`, `RTM_NEWADDR`, and IPv4 `RTM_GETROUTE`/`RTM_NEWROUTE` are implemented; parse-error ACK replies carry the requester's port. |
| Namespaces/procfs | `/proc/<pid>/ns/net` exposes the single synthetic `net:[4026532000]` namespace. `/proc/net` resolves to `self/net`, `/proc/<pid>/net/dev` reports live interface counters, and `ip route show/get/add` works against the main, local, and default tables. |
| PTY | `/dev/ptmx` points at `pts/ptmx`, devpts is mounted on both boot paths, and slave ownership follows the opener. A normal root shell and non-root node-pty both work. |
| Desktop | A compact direct QEMU boot (kernel + full Stage1 initrd + RAM-backed simple-framebuffer DTB) reaches Xorg on `/dev/fb0` at 1280x1024, renders the wallpaper/icons/panel, and opens a visible Firefox window. The host reads the guest framebuffer through the QEMU monitor and validates the pixels. |
| Desktop gate | `make test_riscv_debian_desktop` reaches the ordered `BROWSER_WEB_DESKTOP_STAGE` markers, requires a visible Firefox window (geometry-checked, `xdotool` with `xwininfo` fallback), captures desktop and Firefox frames from guest RAM with `pmemsave`, validates non-black/colour-rich pixels, and requires the Firefox window to change the frame. The prior marker-only gate passed on a dead bochs PCIe BAR and was not display evidence. |
| Browser | Firefox maps its window, opens a Marionette session, launches Gecko content/socket processes, navigates to the local owned fixture page, completes the public Baidu homepage gate, and passes the public Bilibili playback gate. Baidu reports `BAIDU_GATE_RC=0` with `tls=verified`; Bilibili plays a live 640x360 video with 127 decoded frames (`BILIBILI_GATE_RC=0`). |
| dsh | Non-root headless turn, C compile/run through the bash tool, and the non-root web UI (UID 1000, HTTP 200) are verified. |
| ext4/JBD2 | The ext2 driver also registers an `ext4` type with extent-tree mapping and internal-journal replay. QEMU boots a 4 KiB ext4 root, runs GCC and Python from it, passes multi-extent/truncate/link/rename/sync probes, and recovers all four deterministic journal crash stages. The broadened write suite adds random-data SHA-256, a 200-file directory, a 20-level tree, hole reads, `fallocate` allocation, and zero-range verification; a generated 64-bit group-descriptor image passes the same suite and stage 1. Every resulting image passes `e2fsck -fn`. |
| ext2 durability | Block and inode frees validate the whole range, system-zone overlap, bitmap state, and counters before mutating anything; inode reads are bounded by the filesystem inode count; writable remounts require a clean superblock and read-only remounts sync first; renaming between two hard links of the same inode is a no-op. The ext2 write probe and both the standard and 64-bit ext4 gates were rerun on this code; all four crash stages still pass with clean `e2fsck -fn`. |
| Kernel tests | The RISC-V `aster-core` suite reports `180 passed; 0 failed; 0 filtered out`, including the `/proc/net`, IPv4 route, ext4 extent, JBD2, and ext2 durability tests. The full 18-crate suite reports 536 passed and 4 failures outside ext2 (two `ostd` simple-framebuffer metadata tests and two `aster-nvme` block-device tests). The QEMU ext4 gate below adds the crash/replay and `e2fsck` runtime checks. |
| Host tests | The isolated host runner covers the active Debian tooling with 93 tests in 10 modules and no skips; the baseline manifest rejects missing tests, new skips, timeouts, and import failures. |
| Product gate | One aggregate target runs the direct QEMU gate and optionally the ext4/JBD2 and dsh components, writing per-component receipts and a single `result.json`. A real run including dsh passed in 203.8 seconds with gcc 14, Python 3.13.5, Node v24.21.0, git 2.47.3, C compile/run returning 42, and all four ext4 journal stages clean. |

## Cleanup verification (2026-10-30)

The repository-only cleanup removed more than 60 files (about 20,700 lines) from
`tools/riscv/debian/rootfs`: the paused DRM/Megrez/physical-graphics
verticals, the M2-M4 gates, the old host gate modules, and the dead browser
daily-use/diagnostic scripts. The active surface is now the two profiles, the
Stage1 sources, the direct gate, and the browser/desktop runtime. The builder
rejects every profile outside `minimal` and `browser-web`.

Validation performed on the cleaned tree:

- `minimal` and `browser-web` rootfs images rebuilt from the signed indexes and
  passed `contract verify`;
- the full Stage1 initramfs rebuilt with the expected entry list; the current
  Stage1 self-tests pass;
- the direct QEMU gate passes with the toolchain rootfs (`gcc` compile/run,
  Python, Node, git, and dsh help);
- `aster-core` ktests pass: 147 passed, 0 failed, 0 filtered;
- a manual U-Boot boot of the cleaned `browser-web` rootfs reached Xorg,
  openbox, pcmanfm, lxpanel, and a mapped Firefox New Tab window;
- Stage1 devpts works for both root and UID 1000: `pty.fork` ran `/bin/sh` on
  `/dev/pts/0` and exited 0 in both cases;
- QEMU slirp networking works on the cleaned image: the 64 KiB fixture returns
  200 with the exact SHA-256, DNS resolves, direct HTTPS and the host-proxy
  HTTPS both return 200;
- the cleaned `desktop-network-evidence` script completes all ten layers
  (`link`, `address`, `neighbor`, `reachability`, `dns`, `http`, `https`,
  `baidu-asset`, `repeat`, `medium`) and reports
  `DEBIAN_WEB_NETWORK_READY mode=direct layers=10`;
- dsh headless returns the tool result and final text for a bash-tool turn;
  non-root dsh web starts as UID 1000 and serves the authenticated UI with
  HTTP 200.
- The Firefox child-process blocker was traced to the kernel's SCM_RIGHTS
  restriction: passing a UNIX socket over a UNIX socket required
  `CAP_SYS_ADMIN`, so the UID-1000 Firefox parent could not hand IPC sockets to
  its children. The restriction is removed (with the known absence of Linux's
  UNIX-socket cycle collector documented in the kernel). A UID-1000
  SCM_RIGHTS probe now succeeds, Firefox launches its forkserver and socket
  process, and Marionette reads `Asterinas Browser Quality` from the local
  fixture page.

The downstream commit stack was then replayed from `986c70915` onto the latest
`upstream/main` at `cdd5bc109` (upstream added one LoongArch cache-policy fix).
The RISC-V kernel was rebuilt, and the direct gate, 147 kernel ktests,
PTY/network runtime regression, and desktop/Firefox window were rerun on the
rebased tree; all pass consistently.

The QEMU input contract is the fixed `event0`/`event1` pair; the device helper
no longer performs evdev identity ioctls that can block on this kernel. The
network script uses QEMU-safe fixture timeouts and no longer tries to set the
wall clock, which this kernel does not support.

Logs are under the evidence directory:
`clean-build-minimal.log`, `clean-build-browser-web3.log`,
`clean-desktop-capture.log`, `clean-desktop-diag.log`,
`clean-desktop-firefox-late.png`, `ktest-scm-serial.log`,
`direct-gate-scm.log`, `scm-rights-probe.log`, `firefox-content-probe.log`,
`firefox-scm-navigation.log`, `firefox-scm-diag.log`,
`firefox-baidu-scm.log`, `firefox-bilibili-scm.log`,
`scm-regression-test-http2.log`, `net-procfs-probe.log`,
`net-files-probe.log`,
`route-dump-probe.log`, `route-insert-probe2.log`,
`debian-dsh-headless.log`,
`dsh-web-nonroot.log`, `runtime-regression.log`, and
`clean-network-script.log`.

## Public Firefox evidence (2026-10-30)

Both public gates ran on the SCM-fixed kernel and the cleaned `browser-web`
image, with QEMU slirp, guest `10.0.2.15/24`, resolver `10.0.2.3`, and no
content-sandbox environment override:

- `browser-web-marionette-gate --scope baidu-home` returned 0 and printed
  `DEBIAN_BROWSER_WEB_BAIDU_HOME_READY` for `https://www.baidu.com/` with
  `tls=verified` and `title_sha256=b2e9462c8256c220b01b589db1ad6c4e8ebf7a6a65ccfea5811083a8fbbe3825`.
  It wrote `baidu-home.json` (27,293 bytes) and `baidu-home.png` (134,655
  bytes) to the guest evidence directory.
- `browser-web-marionette-gate --scope bilibili-playback` returned 0 and printed
  `DEBIAN_BROWSER_WEB_BILIBILI_PLAYBACK status=pass bv=BV1FRx76SEkz`. The live
  640x360 blob-source video decoded 127 frames (113 dropped under TCG) while
  muted and advanced 0.511 seconds.

The public browser evidence files live inside the guest's snapshot rootfs; the
serial logs remain on the host. The QEMU network and procfs absorption is
recorded separately below.

### SCM_RIGHTS regression coverage

`test/initramfs/src/regression/network/scm_rights_socket.c` creates two UNIX
socketpairs, drops a forked child to UID 65534, passes one socket descriptor
through `SCM_RIGHTS`, and verifies that the parent receives a usable socket.
This is the permanent regression guard for the Firefox content-process IPC
handoff. The test passes its 15 checks on Linux and on the rebased Asterinas
kernel.

### QEMU networking/procfs absorption (2026-10-30)

The rebased kernel now exposes `/proc/net` as a symlink to `self/net`,
implements `/proc/<pid>/net/dev` with per-interface receive/transmit counters,
and provides IPv4 route dumps, lookups, and insertions through rtnetlink.

QEMU verification:

- after a 571,112-byte HTTP transfer, `/proc/net/dev` reported eth0 receive
  and transmit byte/packet counters increasing from zero;
- `ip route show` reported the default route via `10.0.2.2` and the connected
  `10.0.2.0/24` route;
- `ip route get` resolved host, loopback, and added routes;
- `ip route add 192.0.2.0/24 dev eth0` and
  `ip route add 198.51.100.0/24 via 10.0.2.2 dev eth0` succeeded, appeared in
  `ip route show`, and were returned by `ip route get`;
- `/proc/net/route` reported the main-table routes;
- `/proc/net/tcp` reported a live `LISTEN` socket (state `0A`) and
  `/proc/net/udp` reported a bound UDP socket (state `07`).

### ext2 durability absorption (2026-10-30)

Commit `31acb7c3c` ports the old tree's durability invariants into the shared
ext2/ext4 driver:

- `free_blocks` validates the complete range, system-zone overlap, already-free
  bits, and the group/filesystem free counters before changing a bitmap, so a
  rejected cross-group free cannot leave a half-freed prefix;
- inode allocation and free reject bitmap/counter disagreement, out-of-range
  partial-group indices, and filesystem free-inode overflow before mutation;
  allocation also rolls back the bitmap if the computed inode number is
  invalid;
- inode reads reject numbers beyond `s_inodes_count`, and writable remounts
  require a valid, error-free superblock while read-only remounts sync metadata
  and the device first;
- POSIX rename between two hard links of the same inode is a no-op and keeps
  both directory entries.

The accompanying ktests cover the partial last group, already-free block and
inode rejection, reserved-inode allocation rollback, superblock counter
overflow, unclean writable remount, and same-inode rename. All six new tests
and the existing ext2 tests pass (`180 passed; 0 failed` in `aster-core`).

The ext4 write suite and all four journal crash stages were rerun against the
same kernel on both the standard image and the 64-bit group-descriptor image.
Stage 1 discarded its uncommitted transaction, stages 2-4 recovered, and every
resulting image passed `e2fsck -fn`; both gates reported `"status": "pass"`.
The ext2 write probe (1 MiB write, hard link, unlink, rename, and two syncs)
also passed and left a clean `e2fsck -fn` image.

### Host tests and product gate absorption (2026-10-31)

Commit `2abf63a3a` adds the host test runner and product gate. The rebase line
now has a host test runner at
`tools/riscv/host_test_runner.py` and a baseline at
`tools/riscv/tests/expected_host_tests.json`. Each module runs in an isolated
worker process with a timeout; the runner rejects import failures, timeouts,
duplicate IDs, missing baseline IDs, and unexplained skips. The current
baseline is **10 modules / 86 tests / 0 skips**, covering the direct and ext4
gate command contracts, rootfs profiles and manifests, signed sources,
descriptor-pinned publication, builder CLI contracts, and active tooling
syntax. Run it with `make test_riscv_host HOST_TEST_JOBS=32`.

`tools/riscv/debian/product_gate.py` is the aggregate product gate. It requires
a direct-boot rootfs and optionally adds the ext4/JBD2 gate and the dsh probe;
it writes `result.json` plus per-component receipts. A real run used the
2 GiB dev-runtime rootfs, the 3 GiB browser ext4 image, and the dsh repo disk:
the direct gate reported gcc 14, Python 3.13.5, Node v24.21.0, git 2.47.3, a
compiled C program returning 42, and the dsh entry point/usage line; the ext4
gate discarded stage 1 and recovered stages 2-4 with clean `e2fsck`; the
aggregate result passed in 203.8 seconds.
`make test_riscv_debian_full` runs the host suite followed by the product
gate.

The product gate covers the QEMU Debian surface, not the manual U-Boot
desktop/browser scenario, which remains a separate acceptance step.

### Desktop gate consolidation (2026-10-31)

The desktop gate does not use U-Boot or the historical M3/M4 framework. A
direct QEMU boot with the kernel Image, the full Stage1 initramfs, and
`fb-bochs.dtb` reaches systemd, Xorg, the window manager, the desktop session,
and `panel-start`. `tools/riscv/debian/desktop_gate.py` checks those ordered
markers and is exposed as `make test_riscv_debian_desktop`.

This replaced the initially picked generic gate stack. The consolidation
commit `da6542b67` added 369 lines and removed 4,306 lines: the QMP/PPM
helpers, gate protocol/runtime/orchestration framework, systemd M2 gate, and
M3/M4 wrappers are gone.

The later P0 pass showed that the marker-only direct-boot gate was not display
evidence. Direct boot has no firmware to program the bochs PCI BAR; `info pci`
showed BAR0 at `0x0`, and the DTB framebuffer at `0x40000000` read back
`0xffffffff` even while all six markers passed. The gate now builds a
RAM-backed simple-framebuffer DTB (`make_fb_ram_dtb.sh`), boots with
`ASTERINAS_BROWSER_WEB_BASIC_ONLY=1` and
`ASTERINAS_WEB_NETWORK_MODE=direct`, waits for a visible Firefox window from
a root guest observer, and reads the desktop/Firefox frames from guest RAM via
the QEMU monitor. It validates non-black pixels and colour count and requires
the Firefox window to change the frame. Host tests are now 93 tests in 10
modules.

The skill and pick ledger under `.agents/skills/upstream-rebase-cherry-pick/`
and `docs/porting/2026-10-31-cherry-pick-ledger.md` record the pick and the
consolidation decision, including the dead-framebuffer, network-mode, and
process-substitution pitfalls found in this pass.

## Remaining blockers

1. **ext4 feature coverage.** The compatibility driver covers a downstream
   Debian ext4 profile with extents and an internal journal. P1 broadens the
   write suite (200-file directory, 20-level tree, hole reads, random-data
   SHA-256, `fallocate` allocation/zero-range) and adds a generated 64-bit
   descriptor matrix target. It still deliberately rejects metadata checksums,
   high 64-bit block numbers, `flex_bg`, `orphan_file`, inline data, and full
   multi-level extent splitting. The old xfstests suite is not ported; the old
   advanced ext4 feature commits were trial-picked and classified as blocked
   because they depend on a divergent staged-metadata/block-group design (see
   the cherry-pick ledger).
2. **Network/procfs completeness.** IPv4 route dump/lookup/add,
   `/proc/net/route`, `/proc/net/dev`, `/proc/net/tcp`, and `/proc/net/udp`
   are present, but IPv6 route netlink, route deletion, and advanced route
   filters remain unimplemented.
3. **Board support.** EIC7700 cache/PCIe glue and the Megrez serial, storage,
   network, USB, and watchdog drivers are out of this line.
4. **Test surface.** The direct QEMU gate, ext4/JBD2 QEMU gate, aggregate
   product gate, host test suite, desktop Firefox/framebuffer gate, and kernel
   ktests are ported. The U-Boot-specific desktop path remains historical and
   out of the current gate; dsh service/launcher integration is still not
   automated. The ext4 suite remains the P1 item below.

## Current entry points

```bash
# Kernel
RUSTUP_TOOLCHAIN=nightly-2026-07-21-x86_64-unknown-linux-gnu \
  cargo osdk build --target-arch riscv64 --scheme riscv --boot-method vmm-direct

# Rootfs (see tools/riscv/debian/rootfs/README.md for the container invocation)
tools/riscv/debian/rootfs/build_rootfs.sh --profile minimal
tools/riscv/debian/rootfs/build_rootfs.sh --profile browser-web

# Stage1
tools/riscv/debian/rootfs/build_stage1.sh target/debian-riscv/stage1/initramfs.cpio

# Direct Debian gate
make test_riscv_debian_direct \
  TARGET_ARCH=riscv64 \
  DEBIAN_DIRECT_ROOTFS=/absolute/path/to/debian-root.ext2

# The direct gate accepts an ext4 image and selects the ext4 mount explicitly
python3 tools/riscv/debian/direct_boot_gate.py \
  --kernel target/osdk/asterinas-osdk-bin.Image \
  --rootfs /absolute/path/to/debian-root.ext4 \
  --rootfstype ext4

# ext4/JBD2 write plus crash/replay gate
make test_riscv_debian_ext4 \
  DEBIAN_EXT4_ROOTFS=/absolute/path/to/debian-root.ext4

# Derive and exercise a 64-bit-descriptor ext4 image
make test_riscv_debian_ext4_64bit \
  DEBIAN_EXT4_64BIT_BASE=/absolute/path/to/debian-root.ext4

# Host-side tooling suite (isolated modules, baseline-checked)
make test_riscv_host HOST_TEST_JOBS=32

# Aggregate product gate: direct boot plus optional ext4/JBD2 and dsh
make test_riscv_debian_product \
  DEBIAN_PRODUCT_ROOTFS=/absolute/path/to/toolchain-rootfs.ext2 \
  DEBIAN_PRODUCT_EXT4_ROOTFS=/absolute/path/to/debian-root.ext4

# Direct-boot desktop Firefox/framebuffer gate (builds fb-ram.dtb)
make test_riscv_debian_desktop \
  DEBIAN_DESKTOP_ROOTFS=/absolute/path/to/browser-web.ext2 \
  DEBIAN_DESKTOP_STAGE1=/absolute/path/to/initramfs.cpio \
  DEBIAN_DESKTOP_DTB=/absolute/path/to/fb-bochs.dtb

# Kernel tests (run from kernel/core; output lands in qemu-serial.log)
RUSTUP_TOOLCHAIN=nightly-2026-07-21-x86_64-unknown-linux-gnu \
  cargo osdk test --target-arch riscv64 --scheme riscv --boot-method vmm-direct
```

A deterministic JBD2 crash can be requested with
`asterinas.ext4_fault_stage=1..4`. Stage 1 stops after durable descriptor
blocks, stage 2 after the commit record, stage 3 after home-block writes, and
stage 4 after the device flush before the journal checkpoint.

The kernel build requires the freshly built OSDK 0.19 binary from this branch's
`osdk/` source; the container's stock OSDK 0.18 cannot parse `vmm-direct`.
`--scheme riscv` is required to emit
`target/osdk/asterinas-osdk-bin.Image`.

## Evidence

Evidence from the verification runs lives under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-29-upstream-baseline/`:

- `uboot-net-probe.log`, `netlink-set-probe.log` — fixture, DNS, HTTPS, and
  rtnetlink behavior;
- `uboot-pty-probe.log`, `pty-proc-probe.log`, `node-pty-probe.log` — PTY and
  devpts;
- `dsh-web-nonroot.log` — non-root dsh web UI;
- `uboot-desktop-firefox-late.png`, `uboot-firefox-diag.log`,
  `uboot-xorg-probe.log` — Xorg and the Firefox window;
- `uboot-baidu-probe.log`, `uboot-baidu.ppm` — the blocked online navigation
  and Gecko child-process failure;
- `ktest-*-serial.log`, `ktest-netlink-core.log` — kernel tests.

The ext4/JBD2 evidence is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-30-ext4-jbd2/`:

- `ext4-write-probe.log`, `ext4-extent-probe.log` — ext4 root writes,
  fragmented extents, truncate, links, and rename;
- `final-stage1.log` through `final-stage4.log` — the four journal crash
  points and recovery boots;
- `e2fsck-*.txt` — read-only consistency checks for the ext4 and ext2 probes
  and all journal stages;
- `ext2-write-probe.log` — the ext2 write path after the shared durability
  changes;
- `ktest-ext4-serial.log` — the RISC-V ktest serial log before the durability
  port (`174 passed`); the post-port log is in the evidence directory below;
- `README.md` — exact image conversion, commands, and result table.

The repeatable gate receipts are under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-30-ext4-gate/`,
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-30-ext4-gate-many/`, and
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-30-ext4-64bit/`. They
cover the 32-bit-compatible image with a single-file and a 32-file crash
transaction (all four fault stages), plus a 64-bit group-descriptor image
(write suite plus stage 2); all recovery passes end with a clean `e2fsck -fn`.

The ext2 durability rerun is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-30-ext4-durability/`:
`gate-summary.log` is the post-change write/crash-stage receipt,
`crash-stage1.log` through `crash-stage4.log` and `e2fsck-*.txt` are the four
recovery passes, `ktest-full-serial.log` is the full RISC-V ktest output
(`aster-core` 180/180; full suite 536 passed with four failures outside ext2),
and `ext2-write-probe.log` plus `e2fsck-ext2.txt` are the ext2 write pass.
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-30-ext4-64bit-durability/`
holds the post-change 64-bit gate: all four crash stages plus `e2fsck` receipts.

Host-test evidence is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-host-tests/`:
`make-test-riscv-host.log`, the baseline manifest, and per-module JSON/log
files. Full product-gate evidence is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-product-gate-dsh/`:
`result.json`, the parsed `direct-gate.json` / `ext4-gate.json`, their gate
transcript logs, `direct-serial.log`, and the ext4 crash receipts. The
combined host-plus-product invocation is preserved under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-full-gate-committed/`; the
earlier direct+ext4-only run is in the sibling `2026-10-31-product-gate/`
directory. The marker-only desktop gate evidence is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-desktop-gate-make/`
(`result.json`, `desktop-serial.log`, `make-run.log`, `README.md`), but it is
superseded by the P0 gate below.

The Firefox/framebuffer desktop gate evidence is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-p0-desktop-gate-fb5/`:
`log/result.json` has `status=pass`, `changed_fraction=0.993874`, and the
Firefox window marker; `log/desktop-framebuffer.ppm` and `log/framebuffer.ppm`
are the real guest desktop and Firefox frames; `desktop.png` and `firefox.png`
are renderings of those PPMs; `log/desktop-serial.log` is the full marker and
heartbeat transcript. The earlier diagnostic runs are preserved:
`2026-10-31-p0-fbprobe-ram/` (the bochs DTB read `0xffffffff`; the RAM DTB
rendered the wallpaper), and `2026-10-31-p0-desktop-gate-fb4/` (a small
`Firefox` helper window was found before the main window). The
`2026-10-31-p0-firefox-diag/` serial shows the failed browser unit and
`2026-10-31-p0-fbprobe-ram-long/` shows the desktop settle timing.

A fresh `browser-web` rootfs was then rebuilt from the current builder
(`build_rootfs.sh --profile browser-web`, log `target/p0-builder.log`) and the
same Makefile gate passed on it: `status=pass`, `changed_fraction=0.994758`,
title `Mozilla_Firefox`. Evidence is under
`2026-10-31-p0-desktop-gate-fresh/`. The fresh image contains
`desktop-frame-evidence`, its enabled unit, and the `Downloads` directory, so
the P0 result is reproducible from the repository builder rather than only the
earlier patched image.

The P1 ext4 evidence is under
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-p1-ext4-64bit-extended/`
(semantics plus stage 1 on the generated 64-bit descriptor image) and
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-p1-ext4-64bit-make/`
(the real `make test_riscv_debian_ext4_64bit` run). Both contain
`semantics.log`, `crash-stage1.log`, `recovery-stage1.log`, the `e2fsck`
receipts, and the gate receipt. The earlier `2026-10-31-p1-ext4-64bit/` run
is the pre-broadening baseline on the same image.

The old tree remains the fallback for behavior that is not yet reproduced on
this line.

## Scale

`tools/riscv` is now a small Debian-facing surface: the two builders, the
direct gate, the Stage1 C sources, and the current browser/desktop runtime.
The removed verticals were not replaced with stubs; any future board or GPU
work needs a separately approved workstream and its own gate.

## Upstream update and branch management

- `upstream/main` is the only rebase base. Fetch official main over HTTPS
  (SSH fetch can be blocked); do not use `origin/main`, the legacy fork main,
  as a merge base. The base was rechecked against the remote on 2026-10-30 and
  is still `cdd5bc109`.
- The integration branch carries the downstream product commits as one
  rebasable stack. Keep a local backup branch or tag before each rebase and do
  not force-push it without explicit approval.
- Upstreamable fixes belong on short-lived topic branches cut from
  `upstream/main`; the SCM_RIGHTS fix and its regression test are the first
  such candidates. Do not mix them with the Debian rootfs/desktop product
  branch.
- `origin` is for fork backup and review only. Remote CI is not a dependency;
  local builds and QEMU gates are the hard gate. Do not push or open a remote
  PR without explicit authorization.
- The old fork history must not be merged: its merge base with upstream is
  2022 and the trees are structurally different.
