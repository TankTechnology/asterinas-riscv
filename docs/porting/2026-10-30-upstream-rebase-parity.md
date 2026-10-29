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
- Guest networking through QEMU slirp: virtio-net 10-byte header fix, port-0
  privilege fix, non-root HTTP and API access.
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

## Preserved feature areas and status

| Area | Current state on rebased line | Required next work |
|---|---|---|
| Debian rootfs build | Full builder sources ported; minimal and browser-web profiles built and contract-verified; Stage1 full build succeeds | Validate remaining profiles and manifests; rebuild after the browser service path fix |
| QEMU Debian boot | Direct-kernel gate passes for ELF and raw Image | Port bootargs/artifact contracts and a Stage1/systemd gate |
| Stage1 | Full initramfs reaches systemd, `multi-user.target`, and `graphical.target` | Add getty and full desktop gates |
| systemd/journald | systemd-journald is `active (running)`; `/dev/kmsg`, `SO_TIMESTAMP`, boot_id, and `syslog(2)`/`dmesg` are present | procfs/net extras, cgroup events, reboot sync |
| ext4/JBD2 | Not ported; upstream ext2 rejects normal ext4 root | Extent read/write, journal replay, durability tests |
| RISC-V board boot | Image header and raw Image sidecar implemented; QEMU raw gate passes and U-Boot `booti` reaches userspace | DTB/framebuffer parsing, EIC7700 cache/PCIe glue, a green U-Boot scenario |
| Megrez drivers | Not ported | DW APB UART, SDHCI/MMC, DWMAC, USB xHCI/HID, watchdog |
| Desktop/Firefox | Rootfs guest assets ported; browser unit referenced a missing device-access path | Rebuild with the corrected path, then run Firefox/desktop gates |
| dsh | Headless non-root and C build verified | dsh web non-root, launcher PATH/service, reproducible asset provenance |
| Host tests/gates | Not ported | Runner, expected baseline, rootfs/board/browser unit gates |

## Explicitly out of scope

The paused PowerVR/GPU acceleration line and its physical GPU probes are not
part of this parity target and must not be advanced by this work. Existing
non-GPU display/fbdev code is in scope only for the current desktop surface.

## Port order

1. Rootfs builder reproducibility and browser-web profile.
2. Full Stage1 + systemd (journald) on the upstream kernel. **Done.**
3. Remaining Debian-compatibility packages: procfs `/proc/net`, cgroup
   events, PTY/reboot durability.
4. Browser/desktop startup on the rebased kernel (device-access path fixed,
   then Xorg/Firefox gates).
5. ext4 extents/JBD2 and the ext2 durability fixes.
6. DT framebuffer, EIC7700/PCIe glue, and a green U-Boot `booti` scenario.
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
