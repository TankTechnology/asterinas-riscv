# Megrez Bring-up Evidence Index

This index separates curated conclusions from local raw evidence.
Each row identifies the artifact or commit whose boundary was actually observed.
QEMU results are not treated as Megrez board results.

| Stage | Curated result | Verified boundary |
|---|---|---|
| v3 | Local-only raw record: `porting/logs/megrez-v3-20260712-boot1-analysis.md` | Early markers reached the post-`satp` boundary; prepending the Linux Image header had shifted the linked page-table layout. |
| v5 | Local-only raw record: `porting/logs/megrez-v5-bbf65a4064d9-20260714T042718Z/board-v5-analysis.md` | Full frame-allocator initialization completed; the next page-table operation was not yet proven. |
| v6 | Local-only raw record: `porting/logs/megrez-v6-569698e32230-20260714T045344Z/board-v6-analysis.md` | The initial linear map was entered; the finite capture did not prove that it was stuck. |
| v7 | Local-only raw record: `porting/logs/megrez-v7-6b075e73b29c-20260714T050947Z/board-v7-analysis.md` | OSTD initialized after the long linear map, while WDT0 failed as a dependable recovery gate. |
| v8 | Local-only raw record: `porting/logs/megrez-v8-b60ad6cfb3cb-20260714T114751Z/board-v8-analysis.md` | Components completed and the missing `rng-seed` path became the leading source-backed diagnosis. |
| Upstream `ae38e6c6` | Local-only raw record plus [normalized `booti` transcript](ae38-booti-transcript.md) | U-Boot printed `Starting kernel ...`; no Asterinas output or automatic reset appeared in the 120-second passive window. |
| Megrez `6df0f28f` | [Board and bootargs evidence](2026-07-16-megrez-sv48-bootargs.md) | Default Sv48 reached `rootfs is ready`, then `/init` failed with `ENOENT` because stale U-Boot RAM bootargs omitted `init=/init`. |
| QEMU PID 1 `593d5bb19` | [Corrected bootargs evidence](2026-07-16-megrez-sv48-bootargs.md) | Generic U-Boot with corrected RAM and DTB bootargs reached the unique PID 1 marker; this is not Megrez evidence. |
| Direct QEMU 16 GiB `70734c14e` | [Local evidence identity](#tracked-summaries-and-local-raw-evidence) | Four-hart Sv48/Svade reached the userspace marker in direct QEMU; this did not use U-Boot and is not Megrez board evidence. |
| QEMU software recovery `7f691c479` | [Frozen timer and panic evidence](2026-07-18-riscv-software-reboot-qemu.md) | Timer and panic each requested SBI cold reboot and returned through a new OpenSBI/U-Boot firmware cycle. |
| QEMU recovery matrix `2026-10-03` | [Firmware-to-userspace recovery matrix](2026-10-03-qemu-recovery-matrix.md) | Three headless timer recoveries, one virtio-net run, and one QEMU xHCI run all observed a fresh OpenSBI/U-Boot epoch; a forced three-second timeout retained failure evidence and left no QEMU process. These are QEMU virt results, not Megrez hardware evidence. |
| QEMU SiFive-U MMC cross-check `2026-10-03` | [Firmware-to-userspace recovery matrix](2026-10-03-qemu-recovery-matrix.md) | The emulated SD/MMC path reached Asterinas userspace, but the audited payload did not emit the armed recovery marker and therefore failed closed before a second OpenSBI/U-Boot epoch. This is a diagnostic boundary, not a physical-board result. |
| QEMU SiFive-U recovery-enabled MMC cross-check `2026-10-04` | [Firmware-to-userspace recovery matrix](2026-10-03-qemu-recovery-matrix.md) | Replacing only the kernel in a private SD image with an audited Sv39 payload produced two full MMC/ext4 boots and fresh OpenSBI/U-Boot epochs after SBI software recovery in 12.351 s and 12.322 s. This is QEMU evidence, not physical-board qualification. |
| QEMU PCI xHCI input gate `2026-10-04` | [Firmware-to-userspace recovery matrix](2026-10-03-qemu-recovery-matrix.md) | The bounded QEMU gate delivered the exact USB keyboard and mouse sequences through PCI xHCI, input-core, and evdev, recorded the PCI interrupt binding, and completed cleanup. This is virtual xHCI evidence, not EIC7700 DWC3 evidence. |
| QEMU Debian M5 slirp stability diagnostic `2026-10-04` | [Firmware-to-userspace recovery matrix](2026-10-03-qemu-recovery-matrix.md) | Twenty fixed 64 KiB fixture transfers and DNS/HTTPS succeeded through virtio-net/slirp, but the same run recorded atomic-mode, allocator, and block/BIO panics; the gate failed protocol and cleaned up. This is a reproducible QEMU kernel-stability failure, not a physical-board result. |
| QEMU atomic-mode panic follow-up `2026-10-04` | [Atomic-mode panic analysis](2026-10-04-qemu-atomic-panic-analysis.md) | The failed log was mapped to `BioMetadata::wait` and an allocation-recursive panic diagnostic. The diagnostic path was made allocation-free and current `ostd`/`aster-kernel` sources compile for RISC-V; a fresh OSDK image and repeat workload are still required. |
| QEMU current-source Debian M5 rerun `2026-10-04` | [Atomic-mode panic analysis](2026-10-04-qemu-atomic-panic-analysis.md) | The rebuilt source image passed the same bounded Debian M5 slirp/rootfs workload with 20 fixture transfers, DNS/HTTPS, and desktop markers; no atomic-mode or recursive allocator panic appeared. This is QEMU evidence, not physical-board qualification. |
| QEMU current-source SiFive-U virtio initialization fix `2026-10-04` | [SiFive-U virtio initialization fix](2026-10-04-qemu-virtio-init-fix.md) | Deferred virtio-net and virtio-vsock DMA pool allocation until a matching transport is found, while retaining the socket registry. The current-source image passed the bounded SiFive-U UART/timer/MMC/ext4 software-recovery profile and reached the second OpenSBI/U-Boot prompt. |
| QEMU current-source device isolation `2026-10-04` | [Current-source QEMU device isolation](2026-10-04-qemu-device-isolation.md) | The current image passed minimal QEMU virt recovery, then passed separate virtio-net and PCI xHCI legs. A SiFive-U plus xHCI launch was rejected before boot because that machine has no PCI bus; the harness retained the error and cleaned up. |
| QEMU explicit network isolation `2026-10-04` | [Current-source QEMU device isolation](2026-10-04-qemu-device-isolation.md) | The runner now disables QEMU's implicit user network for every non-network device set. Fresh minimal, virtio-net, and xHCI recovery legs all passed with one recovery epoch and complete cleanup. |
| QEMU strict SiFive-U MMC baseline `2026-10-04` | [Current-source QEMU device isolation](2026-10-04-qemu-device-isolation.md) | The SiFive-U model's default U-Boot network fallback is interrupted once per firmware epoch with guarded Ctrl-C; with `-nic none`, MMC/ext4 boot and SBI software recovery passed. This does not validate physical GMAC. |
| QEMU explicit SiFive-U GEM leg `2026-10-04` | [Current-source QEMU device isolation](2026-10-04-qemu-device-isolation.md) | A machine-specific GEM device contract enabled user networking without adding virtio-net. U-Boot completed bounded DHCP/TFTP attempts in both firmware epochs and Asterinas software recovery passed; this remains FU540 firmware evidence, not EIC7700 GMAC kernel evidence. |
| QEMU repeated device matrix `2026-10-04` | [Current-source QEMU device isolation](2026-10-04-qemu-device-isolation.md) | A second minimal/virtio-net/xHCI/SiFive-U MMC/GEM matrix passed with one `booti`, one recovery epoch, bounded recovery times, and complete cleanup for every leg. |
| Megrez PID 1 and recovery `3ef99e6bd` | [Controlled board evidence](2026-07-20-megrez-pid1-recovery.md) | PID 1 entered userspace and completed a 50-byte `write`; the UART log contained no ordinary hello and Asterinas received no framebuffer; a later complete OpenSBI/U-Boot epoch reached the prompt without an external reset during the controlled window. |
| Megrez Sv39 paging contract `b48cfeea3` | [Sv39/Sv48 fault history and Linux comparison](2026-08-25-riscv-sv39-sv48-lessons.md) | A compiled-Sv39 kernel faulted because assembly independently selected Sv48; the single-mode fix subsequently reached OSTD, four harts, MMC, and rootfs on Megrez. |
| Megrez Debian partition install `b48cfeea3` | [Asterinas Debian install evidence](2026-08-25-megrez-debian-install.md) | Asterinas wrote five mismatching 32 MiB chunks to eMMC partition 2, verified them after reboot, then read the full 1 GiB partition and matched the frozen Debian image SHA-256. Debian userspace boot remains the next gate. |
| Megrez Debian two-boot root `b48cfeea3` | [Asterinas Debian two-boot evidence](2026-08-25-megrez-debian-two-boot.md) | Stage1 entered a Debian 13.6 riscv64 Bash root twice; the second Asterinas boot recovered the first boot's synced nonce and created a second-boot probe on the ext2 root. |
| Megrez Debian systemd M2 `6576d661f` | [Asterinas Debian systemd M2 evidence](2026-08-25-megrez-debian-systemd-m2.md) | Asterinas installed and verified the signed 1 GiB root on eMMC partition 2; systemd 257.13 then reached boot 1, requested a userspace reboot, recovered through a new firmware epoch, and produced the persistent boot-2 PASS. |
| Megrez Debian Desktop M4 `f3d9c73fc` | [Desktop applications on QEMU and Megrez](2026-08-26-debian-desktop-m4-apps.md) | Asterinas installed and verified the signed Desktop M4 root, then reached a persistent physical Xorg fbdev session with two xHCI controllers, USB keyboard and mouse, Matchbox, PCManFM, NetSurf, and xterm; the same frozen userspace passed QEMU first. |
| Megrez DWMAC streaming DMA `ed3a6508e` | [DWMAC RX liveness and physical closure](megrez-dwmac-rx-liveness-contract.md#streaming-dma-physical-closure) | Four ordered TCP payloads completed, including 16 MiB with zero retransmissions in that trace; the runner sealed `passed=true` evidence and recovered to a fresh U-Boot prompt. This is a frozen network probe, not Debian/Firefox integration evidence. |
| Megrez browser interaction baseline `0c2da5081` | [RISC-V browser interaction baseline, 2026-09-16](2026-09-16-browser-interaction-performance-baseline.md) | Byte-identical release-kernel QEMU interaction cycles accepted trusted keyboard input but stayed above the 100 ms p95 target, and a dedicated TLB probe located a software-IPI lost wakeup that `269ee6285` later fixed in `main`. The requested greater-than-fivefold everyday-Firefox improvement remains unverified, and no ratio may be assembled from these observations. |
| Merged main `d38a0fb28` | [Merged-main QEMU and physical validation, 2026-09-20](2026-09-20-merged-main-physical-validation.md) | After merging `codex/firefox-daily-use-perf` into `main` (`bb76f8d63`), the four-hart QEMU graphics/control gate passed with the rebuilt Stage1, and the board reached `desktop-ready` in 61.83 s and completed one `qualified=true` daily-use profile with all seven functional groups passing. The board root was not rebuilt, so `main`'s change to the M5 network evidence script is not validated here. This is an integration smoke validation, not a new performance result. |
| Megrez basic-console hard hang `2026-10-03` | [Unrecovered physical incident](2026-10-03-megrez-basic-console-hard-hang.md) | Asterinas reached the basic shell, armed its 90-second software timer, and then stopped producing UART/network output before any fresh OpenSBI/U-Boot recovery epoch. The board is not considered recovered; exact kernel cause remains unconfirmed and requires an operator reset before the next diagnostic boot. |
| Megrez RockOS reachability check `2026-10-07` | [Current RockOS network check](2026-10-07-megrez-rockos-reachable.md) | Serial remained silent, but bounded ICMP/TCP probes and a strict existing-key SSH identity query succeeded. The board was running RockOS 6.6.87 with a live boot ID and approximately 12 hours of uptime; this is not a complete board hang and no reset was issued. |
| Megrez management-chain gate `2026-10-07` | [RockOS control gate](2026-10-07-megrez-management-chain-gate.md) | RockOS remained reachable as UID 1000 and the deployed menu was audited, but the current root/reboot authentication path was unavailable. No U-Boot or Asterinas command was sent; Basic and all hardware probes remain blocked until a fresh UID-0 control receipt exists. |

## Tracked summaries and local raw evidence

The Markdown pages above are reviewable summaries with artifact identities and interpretation limits.
The 264-item manifest at `.local-workspace/manifests/2026-07-18/` covers only the migrated legacy material from `porting/logs/`, `porting/hardware/`, `.local-notes/`, and the known repository-root local files.
Those local files and manifests are intentionally absent from a fresh clone and must pass a separate redaction review before publication.
The same convention covers the 2026-09-16 board raw material: the
`2026-09-16-board-rockos-*` logs, the JIT startup CPU ledger, and the GDB
capture directories are retained untracked in the development checkout beside
the published page, so a fresh clone contains the curated summary only.

The `70734c14e` result remains locally under
`target/megrez-preflight/slow-70734c14e-rerun2-20260717/`. Its
`slow-result.json` and `slow-direct-svade/result.json` both have SHA-256
`911ab4c070a554fd32046d198a39447b981bae765915c3c483bedbefe061ed71`;
`candidate-manifest.json` has SHA-256
`f58fe7c7d285d38c9a5845d3e11bee39f2a6f35ea23cd3ec7bb2910eca219353`.

The `7f691c479` recovery results remain under `target/qemu-uboot/` on the
development host. Both result locations are ignored and neither is part of the
264-item manifest. Their identities and SHA-256 hashes are anchored by this
index and the tracked recovery-evidence page linked above.

## Current status

This file is an append-only evidence ledger, not a live status page. See the
[single live status entry](../README.md) for the current boundary and next gate.
