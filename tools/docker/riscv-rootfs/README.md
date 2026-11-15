# RISC-V Debian rootfs builder image

This image is a dedicated build environment for the Debian `riscv64` rootfs
profiles under `tools/riscv/debian/rootfs`. It is derived from the validated
Asterinas RISC-V cross/DTC development image; the general development image is
not changed.

## Foreign-architecture execution boundary

The host is normally `x86_64`, while debootstrap's second stage and Debian
package maintainer scripts are `riscv64` ELF programs. This image runs them
through an explicit `proot -q qemu-riscv64-static` boundary. The interpreter
emulates one RISC-V user process at a time without registering a host-wide ELF
handler.

This is different from `qemu-system-riscv64`: the latter emulates a complete
RISC-V machine for Asterinas boot tests. The rootfs builder needs both the
user-mode interpreter and the system emulator, but the generated guest rootfs
must not contain `qemu-riscv64-static`.

The entrypoint defaults `ASTERINAS_EXPLICIT_QEMU=1` and verifies `proot` plus
the explicit interpreter before running a command. This path never mounts,
enables, registers, or otherwise changes the host's `binfmt_misc` registration.
An already isolated and audited binfmt tree remains an opt-in compatibility
path: set `ASTERINAS_EXPLICIT_QEMU=0` and expose its read-only tree through
`ASTERINAS_BINFMT_ROOT`. The entrypoint only verifies that path.

## Build the derived image

The default base is the published Asterinas development image:

```bash
make build_riscv_rootfs_image
```

To use a different pinned base or digest, pass it explicitly:

```bash
make build_riscv_rootfs_image \
  RISCV_ROOTFS_BASE_IMAGE=asterinas/asterinas:0.18.0-20260702@sha256:<digest> \
  RISCV_ROOTFS_IMAGE=asterinas/asterinas:0.18.0-20260702-riscv-rootfs
```

The image installs the build-time contract:

- `debootstrap`, `proot`, and `qemu-user-static`;
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

No `--privileged` flag or `/dev` mount is needed for the default explicit-QEMU
mode. The repository is mounted read/write because the rootfs builder publishes
artifacts below `target/`.

```bash
docker volume create asterinas-riscv-rootfs-cache
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" \
  -v asterinas-riscv-rootfs-cache:/root/asterinas/target/debian-riscv/cache \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs --check
```

The check prints `ASTERINAS_RISCV_ROOTFS_ENV_PASS` and
`execution=explicit-proot`, together with the interpreter path and
`host_binfmt=unchanged`. If it fails, fix the image first; do not start a long
debootstrap build with an unverified environment.

## Build a signed rootfs

Run the existing builder through the checked entrypoint:

```bash
docker run --rm --network=host \
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
