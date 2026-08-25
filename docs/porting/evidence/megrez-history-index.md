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
| Megrez PID 1 and recovery `3ef99e6bd` | [Controlled board evidence](2026-07-20-megrez-pid1-recovery.md) | PID 1 entered userspace and completed a 50-byte `write`; the UART log contained no ordinary hello and Asterinas received no framebuffer; a later complete OpenSBI/U-Boot epoch reached the prompt without an external reset during the controlled window. |
| Megrez Sv39 paging contract `b48cfeea3` | [Sv39/Sv48 fault history and Linux comparison](2026-08-25-riscv-sv39-sv48-lessons.md) | A compiled-Sv39 kernel faulted because assembly independently selected Sv48; the single-mode fix subsequently reached OSTD, four harts, MMC, and rootfs on Megrez. |
| Megrez Debian partition install `b48cfeea3` | [Asterinas Debian install evidence](2026-08-25-megrez-debian-install.md) | Asterinas wrote five mismatching 32 MiB chunks to eMMC partition 2, verified them after reboot, then read the full 1 GiB partition and matched the frozen Debian image SHA-256. Debian userspace boot remains the next gate. |
| Megrez Debian two-boot root `b48cfeea3` | [Asterinas Debian two-boot evidence](2026-08-25-megrez-debian-two-boot.md) | Stage1 entered a Debian 13.6 riscv64 Bash root twice; the second Asterinas boot recovered the first boot's synced nonce and created a second-boot probe on the ext2 root. |
| Megrez Debian systemd M2 `6576d661f` | [Asterinas Debian systemd M2 evidence](2026-08-25-megrez-debian-systemd-m2.md) | Asterinas installed and verified the signed 1 GiB root on eMMC partition 2; systemd 257.13 then reached boot 1, requested a userspace reboot, recovered through a new firmware epoch, and produced the persistent boot-2 PASS. |

## Tracked summaries and local raw evidence

The Markdown pages above are reviewable summaries with artifact identities and interpretation limits.
The 264-item manifest at `.local-workspace/manifests/2026-07-18/` covers only the migrated legacy material from `porting/logs/`, `porting/hardware/`, `.local-notes/`, and the known repository-root local files.
Those local files and manifests are intentionally absent from a fresh clone and must pass a separate redaction review before publication.

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
