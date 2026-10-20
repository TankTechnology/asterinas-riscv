# RISC-V Sv39 direct startup revalidation r58

This is a bounded, non-DRM, non-GL, non-Firefox revalidation after the host reboot.

## Inputs

- kernel: `target/osdk/aster-kernel-osdk-bin.Image`
- kernel SHA-256: `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`
- QEMU CPU: `rv64,svpbmt=true,zkr=true`
- QEMU memory/CPUs: `2G`, `SMP=4`
- command bound: `timeout 60s`

## Result

The process exited with code `124` from the explicit 60-second bound (not a guest panic or host crash). The serial log reached these ordered markers:

1. `Enter riscv_boot`
2. `OSTD initialized. Preparing components.`
3. `ASTERINAS_BOOT_PHASE phase=first-kthread-enter`

No Firefox, Xorg, DRM, network, or real hardware path was started. The timeout is therefore recorded as a bounded observation after kernel entry, not as a desktop success.

## Artifacts

- serial log: `/mnt/mydisk/qemu-sv39-cpu-contract-r58/serial.log`
- artifact checksums: `/mnt/mydisk/qemu-sv39-cpu-contract-r58/SHA256SUMS`
- serial SHA-256: `bdca5ce641f8458fd4754b98106f38864940286aee80c143ac044b7a267974a9`
- exit code: `124`
