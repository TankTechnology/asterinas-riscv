# Megrez PowerVR device-tree probe design

The next PowerVR milestone must establish the board resource contract before
touching GPU registers. The prepared Megrez DTB contains an enabled `img,gpu`
node at `0x51400000` with a `0xfffff`-byte register aperture, three clock
references, five reset references, one interrupt, and `dma-noncoherent`.
Neither this DTB nor the successful RockOS GPU run proves that Asterinas has
enabled the GPU's clocks and power domain. An MMIO read on an unpowered device
could hang the selected boot.

The kernel will therefore add an opt-in, **DT-only** diagnostic behind
`asterinas.gpu_dt_probe=1`. It checks the exact prepared-board resource shape
without acquiring `IoMem`, changing clocks or resets, loading firmware, or
registering a DRM node. One bounded `ASTERINAS_GPU_DT_PROBE` log line records
either `status=ready` with address and resource counts, or `status=skipped`
with a specific reason. The ordinary boot remains unaffected.

The probe is a guard, not an acceleration claim. Its unit contract accepts
the known DT shape and rejects changed address, aperture, clocks, resets,
interrupts, DMA policy, or disabled status. A QEMU kernel test verifies that
the diagnostic finds no `img,gpu` node on the virt machine. The RISC-V kernel
build and existing display checks verify that this addition does not disturb
the desktop path. Only a subsequent, separately gated stage may establish
clock/reset ownership and read the hardware BVNC register.

The selected physical boot must preserve the existing root debug console and
RockOS recovery. Its evidence must include the exact kernel/DTB identity,
fresh nonce-framed UID 0 and boot ID, a close/reopen serial check, and the
probe line. No report may equate a successful DT probe with working PowerVR,
Firefox acceleration, or visible HDMI pixels.
