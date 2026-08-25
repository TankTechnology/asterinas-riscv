# Debian RISC-V systemd M2 signed-build evidence

Date: 2026-08-25

Signed-build source commit: `5157f8333`

QEMU gate source commit: `1e7399d4a`

## Result

The separate `systemd-m2` profile completed all eight signed-rootfs build
phases in 88 seconds. The resulting Debian Trixie 13.6 `riscv64` ext2 image
contains Debian's packaged systemd 257.13 and the Asterinas M2 evidence unit.
The public schema-v2 contract validates successfully. The bounded QEMU gate
then booted that image twice through Asterinas and passed its persistent-root
contract.

This document records the signed build and generic QEMU Sv39/SMP=4 evidence.
It does not claim that the Megrez, network, USB, display, or desktop gates have
passed.

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

## Host and QEMU gate

The complete host unit gate passed 128 tests. Shell syntax, native Stage1 C
warnings-as-errors, Python byte compilation, Ruff lint/format, and diff checks
also passed. These checks were run once after the signed build rather than
repeated around the runtime gate.

The runtime used QEMU 10.2.1 in a fresh `--rm --network=none` container. Its
effective machine contract was:

- `virt`, four harts, and 2 GiB RAM;
- `rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true`;
- no display and no NIC;
- a read-only 64 MiB boot disk and one writable copy of the signed ext2 root;
- one QEMU process across the Debian-requested SBI restart.

The end-to-end artifact timestamp envelope was approximately 94 seconds. The
first attempt exposed a host-side serial-cursor defect: the second wait reused
the first U-Boot prompt. Commit `1e7399d4a` made waits start at explicit
transcript checkpoints. The bounded rerun reused all validated inputs and did
not rebuild or download anything.

The complete serial log contains the following ordered milestones:

```text
OpenSBI v1.7
U-Boot 2026.07
Starting kernel ...
DEBIAN_SYSTEMD_M2_READY boot=1 arch=riscv64 release=13.6
OpenSBI v1.7
U-Boot 2026.07
Starting kernel ...
DEBIAN_SYSTEMD_M2_READY boot=2 arch=riscv64 release=13.6
DEBIAN_SYSTEMD_M2_PASS boot=2
```

There are exactly two OpenSBI, U-Boot, and `Starting kernel ...` epochs. The
full transcript contains no M2 FAIL, kernel panic, Oops, or `BUG:` marker. The
published result is `passed: true`, `reason: pass`, profile `systemd-m2`, and
release `13.6`.

The input identities recorded by the gate are:

| Input | SHA-256 |
| --- | --- |
| Asterinas Sv39 Image | `e8a3b155876b0b6cfee59c09ebb0401a50d43f3cbecb63c21fdcd53e7c5ea66c` |
| Stage1 initramfs | `ef6d7555b5d48abc0f89345e51aef414efb040754682d78b1fb86febd02eec0d` |
| U-Boot | `cd1f164d4d6c3493bdceec168d2d066aaa218fe516ea9cd8cbc049427f9b55bc` |
| Four-hart DTB | `3886fd4e5e7f47e3ba1536b3a374f89d4d06cf42f9c3bb5c9038e418ebf9dec9` |
| Signed root image | `9429f1632083ad2387de9699813f2feba4f63143d0710d14f3f0d7429c535463` |
| Manifest | `61312f756f14a4eb85e9ddacbd2c3f6e6f0a91c9042b5e3858fc6e22b1f82fd3` |
| Package lock | `536f9e59e42a74076079308a68e57626764cfb5633bd9de34b7a9d5de5d2d6e9` |
| Package checksums | `799b1c1ad52046c80c4970722b8f7389afe42a790d8f2a9f181b4c31fae4b435` |

The gate outputs are:

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `boot.ext4` | 67108864 | `959f4ea7d067c83f35740a63dca3c270c3862166a4360df0fcfeb234a7fd623b` |
| `debian-root.run.ext2` | 1073741824 | `5e85360f08e1c139c2cc6f50405e019da2f5e5d133e219731150340c2d485fb0` |
| `systemd-m2.serial.log` | 61714 | `82c513303fcb730a47fa6fc7383e2b0f38ab0780ed749b778269ebc9df618831` |
| `result.json` | 1921 | `42a36b10a3a8eda701eef9c75a92f7a8383d6dac1aa75b81a636629174d338be` |

systemd 257.13 reached the M2 evidence service on both boots. Asterinas still
lacks several Linux interfaces that systemd probes: kmod setup, `fs.nr_open`,
kbrequest, cgroup BPF, some libmount watch behavior, configfs, sysusers, and
logind reported errors; `/run/lock` and `/tmp` mount units also failed. These
failures did not prevent `basic.target`, the evidence service, persistent boot
counting, the userspace reboot, or the second-boot PASS. They remain follow-up
compatibility work and are not hidden by this result.
