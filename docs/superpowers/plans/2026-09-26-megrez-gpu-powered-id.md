# Megrez powered GPU ID implementation plan

1. Add a mock CRG/GPU I/O kernel test. It must show the three gate writes,
   ordered five reset deassertions after delays, one ID read, and complete
   restoration. A mismatched ID and an injected reset-write failure must
   still restore; an unexpected initial CRG tuple must make no writes or GPU
   reads. Observe a compile failure first.
2. Implement a safe-Rust I/O adapter for two CRG ranges and a delayed GPU ID
   mapping. Require exact DT and initial CRG state, add the opt-in boot flag,
   and print a bounded serial result. Do not create any DRM node.
3. Run the focused RISC-V QEMU tests and cached kernel build. Check formatting
   and diff, commit and push on `main`.
4. Stage an immutable candidate, verify host/board SHA-256 and U-Boot CRC32,
   then boot with the existing root debug console and recovery timer. Require
   the powered-ID marker, final CRG restoration, fresh root UID/boot ID,
   desktop readiness, and close/reopen serial control. If the GPU MMIO read
   stalls, use the available physical reset after confirming software
   recovery did not complete.

This gate is an identity test only. Firmware, command submission, memory,
cross-device sharing, and Firefox compositing remain separate later work.
