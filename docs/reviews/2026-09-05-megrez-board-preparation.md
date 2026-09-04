# Megrez real-board preparation (2026-09-05)

This record freezes the current pre-board state without opening the board
serial session or writing any U-Boot/eMMC state. The physical device node
`/dev/ttyUSB0` exists, is owned by `root:dialout`, and had no `lsof`/`fuser`
owner during the read-only check. No reset, `booti`, TFTP, YMODEM, or rootfs
installation was attempted.

## Artifact inventory

The browser rootfs is the signed Debian `browser-web` profile (schema 7) and
is exactly 2 GiB. The current contract now accepts only exact 1 GiB or 2 GiB
root images; the profile/manifest still determines which size is valid.

| artifact | path | SHA-256 |
| --- | --- | --- |
| board Sv48 kernel (historical) | `target/firefox-artifacts/asterinas-firefox-7fc8fd4-board-sv48.Image` | `4507a5fc4d8c3fb67a6077a9b0e29ae6b4c9cdf235c76eecd538dd59ad584005` |
| Stage1 initramfs | `target/debian-riscv/stage1/initramfs.cpio` | `d59a60bb57660403a97d4ecc65b5fa4ad1728cf975c1a48fbfa8328367d01db` |
| Megrez DTB | `target/dev-overlays/browser-web/physical-stage1-argv/artifacts/eic7700-milkv-megrez.dtb` | `02a8d43d581b4aa8e957e231ee90eba19ffd7e8cfcf74694e86a1fb9c6b37f17` |
| QEMU virt DTB | `target/qemu-uboot/browser-web-aa6f7533/qemu-virt.dtb` | `bf99e579fa60d930e0c4c862771617bf26ae7a973da35da774e77e4fb151051a` |
| QEMU U-Boot | `target/qemu-uboot/cache/u-boot-build-browser-web-aa6f7533/u-boot` | `fc1cf7429ccd8e4e703b06e4e05b7cdd9f4e35b2f567eb838275cbc5ed27f3b7` |
| browser-web root image | `target/debian-riscv/browser-web/rootfs/debian-root.ext2` | `55c9db46c5ccf425c4d01a1e0bae347fb973bdbce7655200be3acf78c65248ac` |
| rootfs manifest | `target/debian-riscv/browser-web/rootfs/rootfs-manifest.json` | `703016492d0b89be94e6eab35e210650dbf746dd3be708c9a23bd7c669a8eed2` |
| packages lock | `target/debian-riscv/browser-web/rootfs/packages.lock` | `4a415f719af21b81032c26988bf5c11a39f9cf00ee4640208efbd7f8ce9885f1` |
| package checksums | `target/debian-riscv/browser-web/rootfs/source-metadata/package-checksums` | `75a57b17e3fc777e4cb7eefc94c20c16bdda4fb26626c6121ed9df2c139510d1` |
| base InRelease | `target/debian-riscv/browser-web/rootfs/source-metadata/InRelease` | `98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed` |

## Blocking conditions before a physical gate

1. The available board kernel was built at commit `7fc8fd4`, while the
   current source is `83c122c094c81a075ab67d58b46fa82380c4f8bd`. It is kept as
   historical evidence only and must not be presented as a current-source
   board result.
2. A fresh `TARGET_ARCH=riscv64 SMP=4 make kernel` attempt in the pinned
   container stopped before Rust compilation because the container lacked the
   `inferno` crate in offline mode; the online mode stalled at crates.io index
   refresh and was bounded at 180 seconds.
3. QEMU desktop evidence uses a separate Sv39 kernel. The existing schema-2
   browser plan does not represent separate QEMU-Sv39 and Megrez-Sv48 kernel
   identities, so it cannot issue a valid physical permit until a fresh Sv48
   kernel is built (or the dual-path shell contract is extended for the
   browser profile).

## Next safe step

Restore the missing Rust dependency cache (or provide a controlled mirror),
build a fresh Sv48 SMP=4 kernel into a board-qualified path, and then run the
existing QEMU desktop/recovery gates against the exact artifact set. Only a
passing, current-source evidence pair may unlock the serial gate. The board
session must remain fail-closed until then.
