# RISC-V Debian rootfs builder image

This image is a dedicated build environment for the Debian `riscv64` rootfs
profiles under `tools/riscv/debian/rootfs`. It is derived from the validated
Asterinas RISC-V cross/DTC development image; the general development image is
not changed.

## Why binfmt is required

The host is normally `x86_64`, while debootstrap's second stage and Debian
package maintainer scripts are `riscv64` ELF programs. Linux `binfmt_misc` is a
kernel facility that recognizes a foreign ELF format and dispatches it to a
registered interpreter. In this image the interpreter is
`qemu-riscv64-static`, which emulates one RISC-V user process at a time.

This is different from `qemu-system-riscv64`: the latter emulates a complete
RISC-V machine for Asterinas boot tests. The rootfs builder needs both the
user-mode interpreter and the system emulator, but the generated guest rootfs
must not contain `qemu-riscv64-static`.

The entrypoint mounts `/proc/sys/fs/binfmt_misc` when necessary, enables the
`qemu-riscv64` registration, and refuses to run unless the registration is
enabled and has the `F` (fix-binary) flag. `F` makes the registered interpreter
available across the chroot/container boundary used by debootstrap. The
registration belongs to the host kernel at runtime; it cannot be baked into a
Docker image layer.

## Build the derived image

The default base is the local image already used for the RISC-V cross/DTC
workflow:

```bash
make build_riscv_rootfs_image
```

To use a different pinned base, pass it explicitly:

```bash
make build_riscv_rootfs_image \
  RISCV_ROOTFS_BASE_IMAGE=asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  RISCV_ROOTFS_IMAGE=asterinas/asterinas:0.18.0-20260702-riscv-rootfs
```

The image installs the build-time contract:

- `debootstrap`, `qemu-user-static`, and `binfmt-support`;
- Debian's official `debian-archive-keyring` 2025.1 package, pinned by SHA-256,
  and `gpgv` for signed Debian metadata;
- `e2fsprogs`, `cpio`, `curl`, `file`, and `jq` for image construction and
  evidence checks;
- the RISC-V cross compiler and libc headers;
- device-tree tools and `qemu-system-riscv64` for the boot gates.

No proxy, board address, serial device, or generated rootfs is stored in the
image. Pass a proxy only to the individual container invocation when the
network requires it.

## Run the environment check

`--privileged` is needed because the entrypoint may mount and register
`binfmt_misc` in the container's kernel namespace. The repository is mounted
read/write because the rootfs builder publishes artifacts below `target/`.

```bash
docker volume create asterinas-riscv-rootfs-cache
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD:/root/asterinas" \
  -v asterinas-riscv-rootfs-cache:/root/asterinas/target/debian-riscv/cache \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs --check
```

The check prints `ASTERINAS_RISCV_ROOTFS_ENV_PASS` and the registered
interpreter path. If it fails, fix the container runtime first; do not start a
long debootstrap build with an unverified environment.

## Build a signed rootfs

Run the existing builder through the checked entrypoint:

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD:/root/asterinas" \
  -v asterinas-riscv-rootfs-cache:/root/asterinas/target/debian-riscv/cache \
  -w /root/asterinas \
  -e http_proxy="${ASTERINAS_PROXY-}" \
  -e https_proxy="${ASTERINAS_PROXY-}" \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  tools/riscv/debian/rootfs/build_rootfs.sh --profile browser-web
```

The builder still verifies every signed `InRelease` with
`/usr/share/keyrings/debian-archive-keyring.gpg`. The persistent
`target/debian-riscv/cache` volume stores the repository's content-addressed
package evidence; it is safe to reuse it because every entry is admitted by
SHA-256. The output rootfs remains profile-specific and is never overwritten by
the runtime gate.

The image is a host-side build environment. It does not claim that Firefox,
NetSurf, GMAC, desktop rendering, or a physical Megrez boot has passed. Those
claims require their existing QEMU or board gates after a rootfs has been
successfully built.
