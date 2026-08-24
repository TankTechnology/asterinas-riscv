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
