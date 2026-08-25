# Debian RISC-V systemd M2 signed-build evidence

Date: 2026-08-25

Source commit: `5157f8333`

## Result

The separate `systemd-m2` profile completed all eight signed-rootfs build
phases in 88 seconds. The resulting Debian Trixie 13.6 `riscv64` ext2 image
contains Debian's packaged systemd 257.13 and the Asterinas M2 evidence unit.
The public schema-v2 contract validates successfully.

This document records artifact construction only. It does not claim that the
systemd two-boot QEMU or Megrez gates have passed yet.

## Build environment

- Container image:
  `asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached`
- Container digest:
  `sha256:4f054ba7e4d35567cd1b974506ecc6ae4a9e35e52616ca048cf302f8dfca8b23`
- Debian mirror: `https://mirrors.tuna.tsinghua.edu.cn/debian`
- Per-container proxy: `http://127.0.0.1:17892`
- Debian archive keyring: `2025.1`
- Retained `InRelease`: three `GOODSIG`/`VALIDSIG` signatures
- Binfmt: enabled, `/usr/bin/qemu-riscv64-static`, flags `OCF`

The TUNA transfer of the explicit systemd dependency set reached about
16 MiB/s. The only non-fatal build warnings were APT's root fallback for its
`_apt` user, an unavailable `/dev/pts` log, and systemd package-maintainer
scripts warning that proc/sys were not mounted in the build chroot. Package
installation and the final audits still returned success.

## Frozen identity

| Field | Value |
| --- | --- |
| Schema/profile | `2` / `systemd-m2` |
| Suite/release | `trixie` / `13.6` |
| Architecture | `riscv64` |
| Filesystem label | `ASTER_DEBIANM2` |
| Filesystem UUID | `4a5d8b91-2189-44fa-a908-ae88dc76f2a1` |
| Filesystem geometry | 262144 blocks × 4096 bytes (1 GiB) |
| Journal | absent |
| Package lock/checksum rows | 96 / 96 |
| systemd | `257.13-1~deb13u1` |
| systemd-sysv | `257.13-1~deb13u1` |
| dbus | `1.16.2-2` |
| base-files | `13.8+deb13u6` |
| libc6 | `2.41-12+deb13u3` |

The extracted PID 1 is a dynamically linked RISC-V ELF with interpreter
`/lib/ld-linux-riscv64-lp64d.so.1`. Executing it through the host emulator
reports `systemd 257 (257.13-1~deb13u1)`. The image contains the executable
`/usr/lib/asterinas/systemd-m2-evidence`, and
`multi-user.target.wants/asterinas-debian-m2.service` is a relative symlink to
the installed unit. `/etc/machine-id` is empty and mode 0444. The image does
not contain `qemu-riscv64-static`.

## Published artifacts

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `debian-root.ext2` | 1073741824 | `9429f1632083ad2387de9699813f2feba4f63143d0710d14f3f0d7429c535463` |
| `rootfs-manifest.json` | 19282 | `61312f756f14a4eb85e9ddacbd2c3f6e6f0a91c9042b5e3858fc6e22b1f82fd3` |
| `packages.lock` | 2806 | `536f9e59e42a74076079308a68e57626764cfb5633bd9de34b7a9d5de5d2d6e9` |
| `source-metadata/InRelease` | 140416 | `98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed` |
| `source-metadata/package-checksums` | 9046 | `799b1c1ad52046c80c4970722b8f7389afe42a790d8f2a9f181b4c31fae4b435` |

The two public directories are mode 0755 and all five files are mode 0644.
The 1 GiB image remains ignored and is not committed.
