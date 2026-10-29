# Upstream rebase parity ledger

Base: official `upstream/main` at `986c70915` (2026-10-30).
Working branch: `rebase/upstream-20261029`.
Downstream commits on top of the base are intentionally small and local; they
are not pushed to `upstream` or `origin`.

The old product tree remains at
`/mnt/shared/xaj/Program/asterinas/.claude/worktrees/maintainability-phase0`
and is the source of truth for downstream feature behavior until this ledger
is green.

## Completed on the rebased line

- Official upstream RISC-V kernel builds and boots QEMU `virt`.
- Direct Debian rootfs boot with real toolchain checks (gcc compile/run,
  Python, Node, git).
- Non-root dsh headless turn, including a C compile/run through the bash tool.
  The non-root dsh web server also starts (UID 1000), publishes its token URL,
  and serves the authenticated UI with HTTP 200.
- Guest networking through QEMU slirp: virtio-net 10-byte header fix, port-0
  privilege fix, non-root HTTP and API access.
- The manual U-Boot framebuffer boot carries a virtio-net device in the same
  static DTB. The final Image reports `eth0` at `10.0.2.15`, downloads the
  64 KiB owned fixture with the exact SHA-256, resolves `www.baidu.com`, and
  obtains HTTP 200 from both direct slirp and the host proxy.
- `/proc/[pid]/ns/net` is exposed as a stable synthetic `net:[4026532000]`
  symlink. Asterinas still has one global network namespace; the link makes
  the single-namespace contract readable and lets namespace-equality checks
  (including the Firefox gate) proceed.
- `/dev/shm` is created and mounted on both the rootfs first-process path and
  the Stage1 handoff, with sticky 1777 permissions in the Stage1 path. This
  is a prerequisite for Firefox content processes and POSIX shared memory.
- Firefox's Marionette transport, WebDriver session, and window discovery now
  work under the U-Boot desktop boot, but `WebDriver:Navigate` still fails
  because every Gecko child process (`-contentproc`, socket, and utility)
  exits before the IPC handshake. `/dev/shm` is writable and the gate passes
  the network-namespace check, so this is a remaining multi-process/sandbox
  or IPC kernel gap, not a framebuffer or network failure. Evidence:
  `uboot-baidu-probe.log`, `uboot-baidu.ppm`, and the Firefox stderr tail in
  that log.
- PTY userspace wiring is restored. The kernel exposes `/dev/ptmx` as a
  symlink to `pts/ptmx`, mounts devpts at `/dev/pts` on both the rootfs
  first-process path and the Stage1 handoff, and assigns each new slave the
  opener's fsuid/fsgid. A normal U-Boot/Stage1 boot with the asterinas uid
  reports `/dev/pts/0` and exits 0; node-pty from the dsh image also exits
  cleanly. Evidence: `uboot-pty-probe.log`, `pty-proc-probe.log`,
  `node-pty-probe.log`, and `ktest-pty-serial.log`.
- rtnetlink link and address requests are implemented. `RTM_NEWLINK` and
  `RTM_SETLINK` apply the `ifi_flags`/`ifi_change` mask, `RTM_NEWADDR` reports
  `EEXIST` for an address that is already configured, and successful requests
  without response segments are ACKed. Parse-error replies now carry the
  sender's port, so iproute2 no longer blocks on unsupported requests.
  `ip link set eth0 up` returns 0, a duplicate `ip addr add` returns `EEXIST`,
  and an unsupported `ip route add` returns `EOPNOTSUPP` promptly. The
  aster-core ktest run passes 147 tests, including the two new flag-update
  tests. Evidence: `uboot-net-probe.log`, `netlink-set-probe.log`,
  `ktest-netlink-core.log`, and `ktest-netlink-serial.log`.
- Minimal Stage1 devtmpfs/cgroup2 handoff.
- cgroupfs `memory.max` / `memory.high` writable/readable (values stored, not
  enforced yet).
- Full downstream rootfs and Stage1 builder sources and rootfs builder image
  are present on the rebased line. A fresh minimal Debian rootfs was built
  from the signed Debian Trixie indexes, verified by the rootfs contract, and
  booted under the rebased kernel. The full Stage1 initramfs also builds.
- The browser-web rootfs builds from the signed indexes. Its embedded trust
  contract reports `FIREFOX_TRUST_PASS mode=external:...libnssckbi.so
  ca_certificates=148` and `FIREFOX_ONLINE_ROOTFS_PASS`.
- The Linux RISC-V Image header is at `_start`; OSDK emits a raw
  `asterinas-osdk-bin.Image` sidecar with the image-size field patched from
  the ELF `PT_LOAD` extent. QEMU boots that raw image through the full direct
  Debian gate, and a U-Boot `booti` handoff reaches the userspace completion
  line.
- `/dev/kmsg`, a bounded kernel log ring, `SO_TIMESTAMP` negotiation,
  `/proc/sys/kernel/random/boot_id`, and `syslog(2)`/`dmesg` are implemented.
  Under the Stage1 systemd handoff, systemd-journald reaches
  `active (running)`, and both `multi-user.target` and `graphical.target`
  are reached.
- The RISC-V kernel parses a DT `simple-framebuffer` node, reserves it, and
  maps it write-combining (`PBMT_NC`). The framebuffer is exposed through the
  graphics class (`/sys/class/graphics/fb0/device/subsystem`) with
  `FB_VISUAL_TRUECOLOR`, which is what Xorg's fbdev driver requires.
- Manual U-Boot boot with a bochs display and the `fb-bochs.dtb` reaches the
  full desktop: Xorg accepts `/dev/fb0` at 1280x1024, then openbox, pcmanfm,
  and lxpanel start. Both the desktop wallpaper/icons/panel and a Firefox
  `about:blank` window were captured from the QEMU display. The browser unit
  intentionally exits 64 when no network mode is configured; with
  `systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct` (and the basic-only
  profile) Firefox launches and maps its window. Evidence is under
  `/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-29-upstream-baseline/`
  (`uboot-desktop-firefox-late.png`, `uboot-firefox-diag.log`,
  `uboot-xorg-probe.log`).

## Preserved feature areas and status

| Area | Current state on rebased line | Required next work |
|---|---|---|
| Debian rootfs build | Full builder sources ported; minimal and browser-web profiles built and contract-verified; Stage1 full build succeeds | Validate remaining profiles and manifests; rebuild after the browser service path fix |
| QEMU Debian boot | Direct-kernel gate passes for ELF and raw Image | Port bootargs/artifact contracts and a Stage1/systemd gate |
| Stage1 | Full initramfs reaches systemd, `multi-user.target`, and `graphical.target`; the handoff publishes `/dev/ptmx`, mounts devpts, and provides writable `/dev/shm` | Add getty and full desktop gates |
| systemd/journald | systemd-journald is `active (running)`; `/dev/kmsg`, `SO_TIMESTAMP`, boot_id, and `syslog(2)`/`dmesg` are present; cgroupfs exposes `cgroup.events` and `memory.events` | procfs `/proc/net`, reboot sync, memory limit enforcement |
| Kernel networking | virtio-net `eth0` at 10.0.2.15, slirp fixture/DNS/HTTPS, and rtnetlink link/address setters work | Dynamic route insertion, `/proc/net`, broader netlink dumps |
| PTY | devpts is mounted, `/dev/ptmx` is published, and slave ownership follows the opener; non-root `pty.spawn` and node-pty work | No remaining kernel blocker found |
| ext4/JBD2 | Not ported; upstream ext2 rejects normal ext4 root | Extent read/write, journal replay, durability tests |
| RISC-V board boot | Image header, raw Image sidecar, and DT `simple-framebuffer` parsing are implemented; QEMU raw gate passes, U-Boot `booti` reaches userspace, and the manual bochs-display boot reaches the desktop with working virtio-net | EIC7700 cache/PCIe glue, a repository-level U-Boot desktop/network scenario |
| Megrez drivers | Not ported | DW APB UART, SDHCI/MMC, DWMAC, USB xHCI/HID, watchdog |
| Desktop/Firefox | Xorg fbdev accepts the framebuffer; openbox/pcmanfm/lxpanel render, and Firefox maps its New Tab window. The browser helper path and trust contract are fixed. The guest network path needed by the online workload is verified. Marionette reaches and sessions with Firefox | Firefox content/socket/utility child processes fail their IPC launch, so online navigation still fails; investigate the Gecko child-process handshake (sandbox/user-namespace/SCM_RIGHTS), then add input and a repository-level gate |
| dsh | Non-root headless turn, C compile/run, and non-root web UI (UID 1000, HTTP 200) are verified | Launcher PATH/service integration and reproducible asset provenance |
| Host tests/gates | Kernel ktests run on the rebased line (aster-core: 147 passed); the downstream host suite is not ported | Runner, expected baseline, rootfs/board/browser unit gates |

## Explicitly out of scope

The paused PowerVR/GPU acceleration line and its physical GPU probes are not
part of this parity target and must not be advanced by this work. Existing
non-GPU display/fbdev code is in scope only for the current desktop surface.

## Port order

1. Rootfs builder reproducibility and browser-web profile.
2. Full Stage1 + systemd (journald) on the upstream kernel. **Done.**
3. Remaining Debian-compatibility packages: procfs `/proc/net`, cgroup
   events, reboot durability. PTY and rtnetlink link/address setup are
   **done**.
4. Browser/desktop startup on the rebased kernel. The offline desktop,
   Firefox window, guest network path, and Marionette session are **done**;
   Gecko content/socket/utility child-process launch, online navigation,
   input, and the repository gate remain.
5. ext4 extents/JBD2 and the ext2 durability fixes.
6. DT framebuffer and the manual U-Boot `booti` desktop/network scenario are
   **done**; EIC7700/PCIe glue and the repository-level scenario remain.
7. Megrez storage, serial, network, USB and watchdog drivers.
8. Host test suite, expected baseline, and repository guards.

Each step must land with its own gate. The old tree remains the fallback until
the equivalent gate is green on the rebased tree; no feature is declared
preserved merely because its source file was copied.

## Upstream update routine

Use a clean rebased worktree, fetch official main over HTTPS, rebase the small
downstream commit stack, rebuild the kernel, and run the direct Debian gate.
Do not merge the old fork history: its merge base with upstream is 2022 and
the trees are structurally different.
