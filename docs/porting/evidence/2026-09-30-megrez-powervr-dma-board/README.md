# Megrez selected PowerVR DMA-stage boot (2026-09-30)

This is a physical validation of the **CPU-side DMA staging boundary**, not
GPU firmware startup or rendering. The selected main commit was `c9310e0d0`.
RockOS checked the transferred artifact SHA-256 values, and U-Boot checked
their sizes and CRC32 before the non-default `booti`:

| Artifact | SHA-256 |
| --- | --- |
| Release kernel | `96be2a50253d8fabf1a646dd3e75c6aeb2f67db390665893816de99e20b4d27e` |
| Stage1 safe handoff | `666d58af0673e2af52c9f1f36b5438095c759d95b3efcd800180054606bb3d71` |
| Prepared DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` |

The [boot receipt](boot-dma-probe.result.json) records the exact selected
arguments, artifact sizes and CRC32, RockOS boot ID, and root-console entry.
The RockOS default menu entry was not changed. The Asterinas boot ID was
`c932618f-0a0d-47bf-8f5b-bcebf334c1e3`; closing and reopening the stable
serial device returned UID 0 on that same boot. The selected arguments enabled
`asterinas.powervr=1 asterinas.powervr_dma_stage=1` with a 300-second software
recovery timer and the isolated root debug console.

The matching META LDR parser reconstructed four private firmware segments in
`/run`. The staging client verified the firmware/segment manifest hashes before
opening `/dev/powervr-control`. The [device transcript](stage.log) shows all
four checked writes and CPU readbacks, followed by an exclusive-session close
with the clock/reset registers restored. The code, data, coremem-code, and
coremem-data segments used 13, 5, 18, and 3 owned DMA pages, respectively.
No licensed firmware bytes are in this evidence directory.

The desktop service remained active after staging. A separate serial reopen
verified UID 0, the same boot ID, active desktop and browser services, and a
disarmed software watchdog. A temporary DRM/Xorg measurement was later
returned to the default fbdev provider; the [Firefox hot-path follow-up](../../../performance/2026-09-30-megrez-firefox-hotpath.md)
records that result. GPU-side cache visibility, MMU mappings, firmware
handshake, command execution, fences, and pixel readback are **unverified**.

After the DMA session closed, the same selected Asterinas boot completed a
planned safe reboot to U-Boot and RockOS. A fresh RockOS root login returned
boot ID `1ddc7463-0e88-4883-9148-a399d95c2624`; RockOS rechecked the three
artifact hashes before one more non-default Asterinas boot. The [recovery
receipt](boot-dma-recovery.result.json) records that sequence. On the final
Asterinas boot (`1db1377c-c9a4-474e-b2fd-36576ebecb85`), a fresh serial
connection returned UID 0, matching desktop-ready marker, watchdog value 0,
both desktop services active, and Xorg back on `/dev/fb0`. This proves the
software recovery loop for the CPU-side DMA test, not recovery from a hard GPU
hang or a failed firmware handshake.

The installed rootfs still had an older `megrez-safe-reboot` that treated an
already disarmed desktop as past the original 300-second kernel deadline. Its
first invocation failed before stopping services. The repository helper was
updated to allow an explicit manual safe reboot only after a current-boot
desktop-ready marker and disarmed watchdog are checked. The tested helper was
copied to `/run` and completed unit quiescence, zero remaining UID-1000
processes, `sync`, and the return to U-Boot. This fixes the source for future
root images; this boot did **not** replace the installed persistent helper.
