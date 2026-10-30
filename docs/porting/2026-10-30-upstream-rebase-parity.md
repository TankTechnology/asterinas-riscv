# Upstream rebase: current state

Base: official `upstream/main` at `cdd5bc109` (2026-10-30).
Working branch: `rebase/upstream-20261029`.
All downstream commits are local; nothing is pushed to `upstream` or `origin`.

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
| Networking | virtio-net appears as `eth0`; the slirp fixture, DNS, direct HTTPS, and the host proxy path work. `RTM_NEWLINK`, `RTM_SETLINK`, and `RTM_NEWADDR` are implemented; parse-error ACK replies carry the requester's port. |
| Namespaces/procfs | `/proc/<pid>/ns/net` exposes the single synthetic `net:[4026532000]` namespace. `ip route add` is still unsupported but returns `EOPNOTSUPP` promptly. |
| PTY | `/dev/ptmx` points at `pts/ptmx`, devpts is mounted on both boot paths, and slave ownership follows the opener. A normal root shell and non-root node-pty both work. |
| Desktop | Manual U-Boot boot with the bochs framebuffer reaches Xorg on `/dev/fb0` at 1280x1024, then openbox, pcmanfm, and lxpanel. The wallpaper, icons, panel, and a Firefox window were captured from the QEMU display. |
| Browser | Firefox maps its window, opens a Marionette session, launches Gecko content/socket processes, navigates to the local owned fixture page, completes the public Baidu homepage gate, and passes the public Bilibili playback gate. Baidu reports `BAIDU_GATE_RC=0` with `tls=verified`; Bilibili plays a live 640x360 video with 127 decoded frames (`BILIBILI_GATE_RC=0`). |
| dsh | Non-root headless turn, C compile/run through the bash tool, and the non-root web UI (UID 1000, HTTP 200) are verified. |
| Kernel tests | `aster-core` ktests pass (147 tests), including the rtnetlink flag-update cases. |

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

The 27 downstream commits were then replayed from `986c70915` onto the latest
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
`scm-regression-test-http2.log`, `debian-dsh-headless.log`,
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

The guest route table still has no explicit default route and
`ip route add default` returns `EOPNOTSUPP`; slirp nevertheless carries the
public DNS answer and HTTPS traffic from the configured guest address. This is
a working path, not Linux-complete routing. The evidence files above live
inside the guest's snapshot rootfs; the serial logs remain on the host.

### SCM_RIGHTS regression coverage

`test/initramfs/src/regression/network/scm_rights_socket.c` creates two UNIX
socketpairs, drops a forked child to UID 65534, passes one socket descriptor
through `SCM_RIGHTS`, and verifies that the parent receives a usable socket.
This is the permanent regression guard for the Firefox content-process IPC
handoff. The test passes its 15 checks on Linux and on the rebased Asterinas
kernel.

## Remaining blockers

1. **ext4/JBD2.** The upstream ext2 path rejects a normal ext4 root. Extent
   read/write, journal replay, and durability are not ported.
2. **Network/procfs completeness.** `/proc/net` is absent and dynamic route
   insertion is not implemented. The current fixture and direct HTTPS paths do
   not depend on them.
3. **Board support.** EIC7700 cache/PCIe glue and the Megrez serial, storage,
   network, USB, and watchdog drivers are out of this line.
4. **Test surface.** Only the direct QEMU gate and the kernel ktests are
   ported. The old host test suite, repository-level U-Boot desktop scenario,
   and dsh service/launcher integration are not.

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

# Kernel tests (run from kernel/core)
cargo osdk test
```

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

The old tree remains the fallback for behavior that is not yet reproduced on
this line.

## Scale

`tools/riscv` is now a small Debian-facing surface: the two builders, the
direct gate, the Stage1 C sources, and the current browser/desktop runtime.
The removed verticals were not replaced with stubs; any future board or GPU
work needs a separately approved workstream and its own gate.

## Upstream update routine

Use a clean rebased worktree, fetch official main over HTTPS, rebase the small
downstream commit stack, rebuild the kernel, and run the direct Debian gate.
Do not merge the old fork history: its merge base with upstream is 2022 and the
trees are structurally different.
