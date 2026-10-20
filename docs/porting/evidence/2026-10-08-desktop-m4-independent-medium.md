# Independent `desktop-m4` medium (2026-10-08)

This is a prepared, unpublished physical-board candidate. It does not modify
the RockOS/U-Boot default entry and has not been booted on the board. The
machine-readable plan is
`target/megrez-desktop-m4-independent/plan-20261008.json` and the derived
rootfs is under `target/dev-overlays/desktop-m4-physical-20261008/rootfs/`.

The frozen inputs are:

| Artifact | SHA-256 | Load address |
| --- | --- | ---: |
| Asterinas kernel Image | `d6554801dcb9e902ee773fa3b2c781d149aa969ca4d8aead1e439d350e1f5832` | `0x80200000` |
| Debug stage-1 initramfs | `16c6558760b92e16e605edfd1b52eef5eee9d9a188dc7502a0343f7815771cc4` | `0x83000000` |
| Megrez DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` | `0xf0000000` |
| Derived desktop-m4 root image | `99f5b922636c8c42da77ccf1f4cd066ee7c5cb49083efff322c71d39c6da45c7` | MMC candidate only |

The boot plan uses Sv39, SMP=4, and the 300-second
`asterinas.reboot_after=300` recovery timer. Its required markers are the
kernel handoff, UID-0 debug-console readiness, and the desktop marker. The
root image is derived from the frozen `desktop-m4-explicit7` rootfs through a
content-addressed overlay; the overlay manifest records every replaced file
and its hash.

The QEMU desktop gate has not yet passed the host-visible scanout check, so
this medium remains a candidate only. The next physical action, after the
QEMU recovery gate passes, is a single watchdog-protected boot with no DRM,
GPU, GMAC, DWC3, Firefox, or network workload. Failure at UART, framebuffer,
or watchdog timeout stops the run and returns to the existing RockOS entry.
