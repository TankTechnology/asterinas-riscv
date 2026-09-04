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
| board Sv48 kernel (current source) | `target/firefox-artifacts/asterinas-firefox-current-board-sv48.Image` | `531f74ec0eb26f5854146116edde69d58b400418f61dfdbe68c924811609673f` |
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

1. The historical board kernel was built at commit `7fc8fd4`; it remains
   evidence only and is not used for the next run. A fresh Sv48 SMP=4 kernel
   has now been built from the current source commit and copied to the
   `current-board-sv48` path above.
2. The dependency cache was repaired from the local `with-cargo-cache` and
   `nixos-build` images (including the smoltcp and rust-ctor Git refs), so the
   build reached `aster-kernel`. It exposed a merge regression: the
   source still imported the deleted `START_TIME_AS_DURATION`. That source
   regression is fixed. A second runtime regression was isolated in the
   periodic coarse-clock callback: publishing the coarse snapshot into the
   vDSO VMO from the RISC-V softirq can block first-userspace scheduling. The
   callback now updates only the syscall-side snapshot; vDSO is still refreshed
   during first-kthread initialization and explicit wall-clock changes.
3. The OSDK nested metadata issue is fixed by invoking `cargo metadata` with
   `--no-deps --locked`; the current Sv48 build now completes. QEMU desktop
   evidence still uses a separate Sv39 kernel. The fresh Sv48 candidate above
   passed `megrez-sv48-svade-fast` with `BOOT_COMPLETED` in
   `target/qemu-uboot/megrez-firefox-board-preflight-8e09fc796/qemu`.
   The existing schema-2 browser plan still does not represent separate
   QEMU-Sv39 and Megrez-Sv48 kernel identities, so it cannot issue a physical
   permit until the dual-path shell contract is extended for the browser
   profile.

## Next safe step

The current Sv39 QEMU network gate passes with the coarse-clock fix: 20/20
owned-fixture requests, all ten protocol layers, and `multi-user.target` plus
Xorg startup. The fresh Sv48 candidate also passes the four-hart Megrez
contract simulation. The remaining preparation work is to extend the board
permit metadata with the separate Sv39/Sv48 identities and to bind the exact
Sv48 image, Stage1, and board DTB hashes into one run manifest. Only that
passing, current-source evidence pair may unlock the serial gate. The board
session remains fail-closed until then.

## Firefox startup evidence

The bounded QEMU startup profiler was rerun with `--boot-timeout 360 --smp 4`.
It reached all four startup boundaries, including Marionette readiness, in
223.826 seconds and exited cleanly:

```text
STARTUP_PROFILE_MARKER name=basic elapsed=53.713
STARTUP_PROFILE_MARKER name=x-socket-ready elapsed=79.564
STARTUP_PROFILE_MARKER name=firefox-exec elapsed=80.674
STARTUP_PROFILE_MARKER name=marionette elapsed=223.826
STARTUP_PROFILE_DONE elapsed=223.826 bytes=121737
```

This is a startup-compatibility pass, not a Baidu-content pass. The subsequent
proxy browser gate reached `BOOT_MARIONETTE_CONNECTED`, created a WebDriver
session, and completed the network checks, but the first `GetTitle` command
after Baidu navigation exceeded the remaining bounded gate time. The physical
run therefore remains limited to the desktop/Firefox-window target until that
content-process phase is separately closed.

## Board transfer bundle

The current-source board artifacts were staged without copying the 2-GiB
rootfs image:

`target/firefox-artifacts/board-bundle-20260905/SHA256SUMS`

The bundle contains the current Sv48 kernel, current Stage1 initramfs, and the
Megrez DTB. The rootfs remains at its immutable signed path above; its hash is
bound in this document and must be checked immediately before any transfer.
