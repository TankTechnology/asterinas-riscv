# Debian RISC-V persistent-root gate

This workflow builds one signed Debian Trixie `riscv64` ext2 image and then
boots it twice on current Asterinas. The first boot writes and syncs a random
nonce; the second boot must read the same nonce from the same writable root
disk. The runtime is headless, has four harts, and uses `-nic none`.

Run all commands from the repository root. The validated development image is
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached`.

## Proxy and container setup

Check Clash without changing Docker, apt, Cargo, or Git configuration:

```bash
export ASTERINAS_PROXY=http://127.0.0.1:17892
curl --proxy "$ASTERINAS_PROXY" --fail --head \
  https://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/InRelease
```

Pass the proxy only to this container invocation:

```bash
docker run --rm -it --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -e http_proxy="$ASTERINAS_PROXY" -e https_proxy="$ASTERINAS_PROXY" \
  -e HTTP_PROXY="$ASTERINAS_PROXY" -e HTTPS_PROXY="$ASTERINAS_PROXY" \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached
```

Inside the container, install the build, signature, filesystem, and emulation
dependencies. This does not weaken Debian signature verification.

```bash
apt-get update
apt-get install -y --no-install-recommends \
  debootstrap qemu-user-static binfmt-support debian-archive-keyring \
  gcc-riscv64-linux-gnu libc6-dev-riscv64-cross \
  linux-libc-dev-riscv64-cross cpio e2fsprogs curl gpgv device-tree-compiler \
  qemu-system-misc
update-binfmts --enable qemu-riscv64
cat /proc/sys/fs/binfmt_misc/qemu-riscv64
```

The final read-only check must show `enabled`, the
`qemu-riscv64-static` interpreter, and the `F` flag. If the registration is
already supplied by the host kernel, do not replace it with handwritten
binfmt magic.

## Build the frozen root once

The default HTTPS mirror is TUNA:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh
```

If TUNA fails, retry explicitly with USTC, then official Debian. Do not persist
either URL in system apt configuration.

```bash
tools/riscv/debian/rootfs/build_rootfs.sh \
  --mirror https://mirrors.ustc.edu.cn/debian
tools/riscv/debian/rootfs/build_rootfs.sh \
  --mirror https://deb.debian.org/debian
```

The signed output is under `target/debian-riscv/rootfs/`. Reuse it while this
verification succeeds; do not rebuild merely to repeat a test:

```bash
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/rootfs/packages.lock
```

Build the separate schema-v2 systemd profile only when the M1 artifact is not
the intended input. It has a distinct label, UUID, and output directory, so it
cannot alias the interactive root:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile systemd-m2
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/systemd-m2/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/systemd-m2/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/systemd-m2/rootfs/packages.lock
```

The M2 profile installs Debian's packaged systemd as PID 1, a deterministic
serial evidence service, and no `qemu-riscv64-static` guest binary. Stage1 must
receive the exact init argument `--root-init=systemd`; the gate places it after
the kernel command-line `--` separator so Asterinas forwards it as init argv.

## Build current-main boot artifacts

Build the current Sv39/SMP=4 kernel and deterministic stage-1 handoff archive:

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/riscv/debian/rootfs/build_stage1.sh \
  target/debian-riscv/stage1/initramfs.cpio
```

Prepare the pinned U-Boot build and an exact four-hart QEMU DTB. This bootstrap
disk is only a convenient producer of U-Boot and the DTB; the Debian gate
builds and validates its own three-file boot disk.

```bash
ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/debian-root" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

## Test and run

The unit gate is local and does not launch QEMU or use the network:

```bash
make test_riscv_debian_rootfs_unit
```

Run the explicit two-boot gate as root in the development container:

```bash
make test_riscv_debian_rootfs_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/rootfs/source-metadata/package-checksums" \
  DEBIAN_GATE_OUTPUT="$PWD/target/debian-riscv/gate"
```

For the systemd M2 profile, use the M2 root and Stage1 archive. This gate keeps
one QEMU process alive across the guest's normal reboot, interrupts the second
U-Boot autoboot, and launches Asterinas a second time without `saveenv`:

```bash
make test_riscv_debian_systemd_m2_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/debian-root/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/systemd-m2/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/systemd-m2/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/systemd-m2/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/systemd-m2/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/systemd-m2/rootfs/source-metadata/package-checksums" \
  DEBIAN_SYSTEMD_M2_GATE_OUTPUT="$PWD/target/debian-riscv/systemd-m2/gate"
```

The target requires every frozen artifact and never invokes debootstrap, a
mirror, or `build_rootfs.sh`. Network is permitted only while constructing the
signed base root. QEMU is always launched with `-nic none`.

## Evidence and cleanup

Inspect the immutable provenance and the current-run evidence:

```bash
python3 -m json.tool target/debian-riscv/rootfs/rootfs-manifest.json
sed -n '1,20p' target/debian-riscv/rootfs/packages.lock
sed -n '1,200p' target/debian-riscv/gate/boot1.serial.log
sed -n '1,200p' target/debian-riscv/gate/boot2.serial.log
python3 -m json.tool target/debian-riscv/gate/result.json
python3 -c 'import json; print(json.load(open("target/debian-riscv/gate/result.json"))["final_root_sha256"])'
```

`debian-root.ext2` is immutable. Only the run-private
`debian-root.run.ext2` is writable. A successful run retains both logs, the
writable root, boot disk, and `result.json`. A failed run retains the complete
available logs and a failing result, never a stale `passed: true`, and never
mutates the base image.

## Verified M1 evidence (2026-08-24)

The documented Make target passed on source commit `d50b17aef` in container
image `sha256:4f054ba7e4d35567cd1b974506ecc6ae4a9e35e52616ca048cf302f8dfca8b23`
with QEMU 10.2.1. The runtime container used `--network=none`.

Frozen inputs:

| Artifact | SHA-256 |
| --- | --- |
| Asterinas Sv39/SMP=4 kernel | `9b0b352bc5f3fb38c7d4ee67f3abc40785f3a26861032944a67db8a38da63b60` |
| stage-1 initramfs | `aef46a338a158dbb9fbe4ed220167eb95c6a392c9f17de15d3877230eb740b08` |
| four-hart QEMU DTB | `3886fd4e5e7f47e3ba1536b3a374f89d4d06cf42f9c3bb5c9038e418ebf9dec9` |
| U-Boot | `cd1f164d4d6c3493bdceec168d2d066aaa218fe516ea9cd8cbc049427f9b55bc` |
| immutable Debian root | `060f613281f2e77fa2232f31322213a310f48b5b18df2991ade9eb2fca7bebae` |
| rootfs manifest | `6f246da49af0759184b47867047bec2e73d0be7228f08d0f723ce248412a14ae` |
| package lock | `fd817c8db7bd71098113b8c2ed4f52c3d70542efaaf97ced2b81637c5528dfff` |
| signed TUNA Trixie `InRelease` | `98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed` |

The manifest records Trixie `13.6`, `riscv64`, mirror
`https://mirrors.tuna.tsinghua.edu.cn/debian`, and build timestamp
`2024-01-01T00:00:00Z`. The checked identity packages were
`base-files=13.8+deb13u6`, `libc6=2.41-12+deb13u3`,
`bash=5.2.37-2+b9`, `coreutils=9.7-3`, and `util-linux=2.41-5`.

The final result reported `passed: true`, reason `pass`, two QEMU argv vectors,
boot-one duration 14.042 seconds, boot-two duration 13.940 seconds, and final
writable-root SHA-256
`f6300db673c17c038a8bdbca76f092e891d0874b6d74737e3bb766bbe7492262`.
Both logs contain `__DEBIAN_ROOTFS_SHELL_READY__`, zero command statuses, and
the second-boot probe; nonce plaintext is replaced by `<nonce-redacted>`.

This evidence proves the generic QEMU Sv39/SMP=4 two-boot persistence
contract. It does not claim physical Megrez operation, guest networking,
systemd boot, display, USB, or desktop support.
