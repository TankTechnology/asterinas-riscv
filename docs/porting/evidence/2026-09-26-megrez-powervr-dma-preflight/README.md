# Megrez PowerVR DMA preflight

This is a **Task 3 preflight**, not firmware startup or GPU rendering. A
selected Asterinas boot with `asterinas.powervr=1` and
`asterinas.powervr_dma_probe=1` opened the root-only PowerVR control node.
The owner allocated one pinned, zero-initialized page through `DmaCoherent`,
converted it to an uncached CPU mapping, and verified a CPU write/readback.
The exact [artifact hashes and outcomes](result.json) and
[selected serial events](selected-events.log) are recorded without firmware
binaries or credentials.

The allocated CPU physical and device addresses both read `0xde203000`; the
uncached Die 0 alias was `0xc05e203000`. The address is within the 40-bit GPU
DMA limit and the Die 0 DRAM range. The identity mapping agrees with the
pinned RockOS [`eswin_cpu/sysconfig.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/system/eswin_cpu/sysconfig.c),
which sets a 40-bit DMA mask and uses an identity UMA physical heap. This
probe establishes the allocator and CPU mapping contract; **the GPU has not
read the page**, so device-side address translation and cache visibility are
not yet proven.

The focused [RISC-V QEMU test](qemu-test.txt) rejects nonidentity addresses,
out-of-range allocations, and invalid sizes. On the board, the control node
closed with CRG readback restored to ACLK `0x20`, CFG `0`, GRAY `0`, RESET `0`.
The serial connection was closed and reopened, proving UID 0 and the same boot
ID. The software recovery watchdog was disarmed only after root control was
established. The Firefox desktop service later reached `running` with zero
restarts, but no monitor pixels were captured.

The first selected attempt reached the GPU owner but a long UART command lost
bytes while the desktop was starting. The candidate reset to U-Boot; an
explicit `run bootcmd_rockos` restored the unchanged RockOS `6.6.87` path and
root UART control. The retry used shorter serial commands and deferred desktop
readiness until after the GPU control check.

The [follow-up result](followup-result.json) records a second, separate board
session. Both BVNC-matched RockOS firmware files were copied to the selected
Asterinas ext2 image and verified there by SHA-256; no firmware was executed.
The first boot after staging found Firefox profile metadata corruption:
`prefs.js` pointed to a directory, `user.js` and `.startup-incomplete` pointed
to deleted inodes, and several profile files shared allocated blocks. The
root-only serial channel remained usable. On RockOS, the unmounted 4 GiB
partition was copied to an external image and matched by SHA-256 before
repair. See the [initial read-only fsck](fsck-task3-readonly.log),
[repair log](fsck-task3-repair.log), and [clean postcheck](fsck-task3-postcheck.log).
The repaired image booted Asterinas with Firefox PID 89, Xorg and Openbox;
the GPU DMA lease opened and closed again with the original CRG readback.
After a controlled reboot to RockOS, [read-only fsck](fsck-task3-after-clean-boot.log)
again returned zero. RockOS's RTC was behind the Asterinas boot time, so that
last log reports a future superblock timestamp but no structural errors.
The board was then returned to the selected Asterinas image: fresh boot ID
`57416ac8-c26d-4e85-8cfa-f8fef9e0bf08` retained root control across a
serial close/reopen, with Firefox, Xorg, and Openbox processes running. The
persistent boot default remains RockOS.

The ext2 corruption cannot be attributed to the firmware copy or the DMA
preflight from this evidence. The earlier indexed-directory fix did not prevent
all profile corruption; the raw partition image is retained while the cause is
investigated. Task 3 still needs owned GPU page tables, firmware buffers, a
bounded handshake, and GPU-side visibility before any command submission.
