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
  are present on the rebased line. A minimal rootfs build is the first
  reproducibility check for this port.

## Preserved feature areas and status

| Area | Current state on rebased line | Required next work |
|---|---|---|
| Debian rootfs build | Full builder sources ported; minimal build under validation | Validate browser-web build, profiles, manifests, dev overlay |
| QEMU Debian boot | Direct-kernel gate passes | Port bootargs/artifact contracts and Stage1/systemd gate |
| Stage1 | Minimal builder and upstream kernel handoff | Merge full builder with upstream devtmpfs; getty/journald gate |
| systemd/journald | Handoff reaches systemd, journald still failing | `/dev/kmsg`+syslog, procfs sysctl/net, cgroup events, reboot sync |
| ext4/JBD2 | Not ported; upstream ext2 rejects normal ext4 root | Extent read/write, journal replay, durability tests |
| RISC-V board boot | No Image header/U-Boot `booti` path on upstream | Image header, DTB/framebuffer parsing, EIC7700 cache/PCIe glue |
| Megrez drivers | Not ported | DW APB UART, SDHCI/MMC, DWMAC, USB xHCI/HID, watchdog |
| Desktop/Firefox | Rootfs guest assets ported; kernel framebuffer/input gaps remain | RISC-V simple-framebuffer, desktop/browser gates |
| dsh | Headless non-root and C build verified | dsh web non-root, launcher PATH/service, reproducible asset provenance |
| Host tests/gates | Not ported | Runner, expected baseline, rootfs/board/browser unit gates |

## Explicitly out of scope

The paused PowerVR/GPU acceleration line and its physical GPU probes are not
part of this parity target and must not be advanced by this work. Existing
non-GPU display/fbdev code is in scope only for the current desktop surface.

## Port order

1. Rootfs builder reproducibility and browser-web profile.
2. Full Stage1 + systemd (journald) on the upstream kernel.
3. Kernel Debian-compatibility packages: `/dev/kmsg`/syslog, procfs identity
   and `/proc/net`, cgroup events, PTY/reboot durability.
4. ext4 extents/JBD2 and the ext2 durability fixes.
5. RISC-V Image header, DT framebuffer, EIC7700/PCIe glue.
6. Megrez storage, serial, network, USB and watchdog drivers.
7. Desktop/Firefox and Megrez deployment gates.
8. Host test suite, expected baseline, and repository guards.

Each step must land with its own gate. The old tree remains the fallback until
the equivalent gate is green on the rebased tree; no feature is declared
preserved merely because its source file was copied.

## Upstream update routine

Use a clean rebased worktree, fetch official main over HTTPS, rebase the small
downstream commit stack, rebuild the kernel, and run the direct Debian gate.
Do not merge the old fork history: its merge base with upstream is 2022 and
the trees are structurally different.
