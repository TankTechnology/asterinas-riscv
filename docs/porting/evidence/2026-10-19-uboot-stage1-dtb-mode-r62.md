# U-Boot/Stage1 DTB-mode comparison r62

This is one bounded, no-Firefox U-Boot run using the QEMU-generated `sv57` DTB. The existing r57 run is the `sv39` control.

| Boundary | r57 U-Boot Sv39 | r62 U-Boot Sv57 |
|---|---|---|
| `Starting kernel` | yes | yes |
| `Enter riscv_boot` / `OSTD initialized` | yes | yes |
| `ASTERINAS_FIRST_PROCESS_DIAG stage=stdio_init_ready` | yes | yes |
| `DEBIAN_STAGE1_PROGRESS step=handoff-done action=chdir` | yes | yes |
| `DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec` | yes | yes |
| desktop readiness marker | absent within r57 bound | absent within 90 s bound |

The r62 overall desktop gate is `passed=false, reason=protocol`, but the single assertion here is the kernel-to-Stage1 handoff. Both DTB modes reach the same Stage1 `exec` boundary. The missing desktop marker is therefore a later userspace readiness issue, not an early DTB CPU-validation failure.

## r62 artifacts

- serial: `/mnt/mydisk/qemu-dtb-stage1-compare-r61-sv57-r2/desktop-m4.serial.log` (SHA-256 `7e2ddede8df5cb78d590b5acff963840154535b3d7d7e934bc0190f3c4d32376`)
- result: `/mnt/mydisk/qemu-dtb-stage1-compare-r61-sv57-r2/result.json` (SHA-256 `182cb57405ab560ce294d0ebe2ef6cf8abfdc1e3731c27f843bc7df2acd17f5c`)
- lifecycle: `/mnt/mydisk/qemu-dtb-stage1-compare-r61-sv57-r2/lifecycle.json` (SHA-256 `a809b8f90e3c675e7bd7eb138169a214836681f631e292f046f210782a0fcc4e`)
- boot disk: `/mnt/mydisk/qemu-dtb-stage1-compare-r61-sv57-r2/boot.ext4` (SHA-256 `01e3be7896301be10ca3d34580a9d57dd46e4be474945f5f805c8d2c935d7301`)
- run root image: `/mnt/mydisk/qemu-dtb-stage1-compare-r61-sv57-r2/debian-root.run.ext2` (SHA-256 `de6d3291b34114e987c314a25d7eabab56c133ce0bd65b9b3c479603b0b30845`)
