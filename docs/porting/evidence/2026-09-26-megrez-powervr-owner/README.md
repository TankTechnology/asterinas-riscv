# Megrez PowerVR selected-boot owner

Task 2 was tested on the Megrez board with a selected Asterinas Image. The
persistent RockOS default remained `6.6.87`; the selected boot entered
Asterinas with `asterinas.powervr=1`. The exact Image, DTB, and Stage1 hashes,
boot ID, stable UART device, and outcomes are in [result.json](result.json).
U-Boot checked each artifact's length and CRC32 after loading, and the files
were SHA-256 checked on RockOS before reboot.

The [selected boot events](selected-events.log) show exact GPU DT validation,
the initially powered-off CRG snapshot, BVNC `30.3.408.101`, and registration
of `/dev/powervr-control`. Opening that node as root powered the GPU and held
the existing MMIO owner; another open returned `EBUSY`. A duplicate file
descriptor held the lease until its final close. The subsequent open succeeded.
The final close logged **actual CRG register readback**: ACLK `0x20`, CFG `0`,
GRAY `0`, RESET `0`, equal to the initial snapshot. The node was mode `0600`.
Even with a temporary `0666` mode, UID 1000 was rejected with `EPERM` by the
initial-namespace capability check; the mode was restored to `0600`.

Five focused [RISC-V QEMU kernel tests](qemu-tests.txt) passed, covering absence
of a Megrez node, exact DT shape, exclusive lease, restoration, and failure
poisoning. The final RISC-V kernel build passed. On the board, nonce-framed
serial commands proved UID 0 and boot ID
`06f54f52-6675-4648-a165-f5f69c6c0eea`; after closing and reopening the
stable UART, the same boot ID and root control path were confirmed. A separate
post-test reconnection again found UID 0, the same boot ID, mode `0600`, and
the software reboot watchdog disarmed. The Firefox desktop service remained
running at PID 90 with zero restarts, and Xorg's log reported 1920×1080.
There was no HDMI capture, so actual monitor pixels were not verified.

This is a power and ownership gate. It does not start PowerVR firmware,
allocate GPU DMA memory, expose a PowerVR render node, run a GPU pixel test,
or accelerate Firefox. Those remain [Tasks 3–5](../../../superpowers/plans/2026-09-26-megrez-powervr-minimal-render.md).
