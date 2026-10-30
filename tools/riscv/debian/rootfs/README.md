# Debian RISC-V rootfs builder

This directory builds the two Debian rootfs profiles used by the upstream-rebase
line and the Stage1 handoff that boots them. Both profiles are ext2 images with
a reproducible filesystem identity:

| Profile | Label | Size | Purpose |
|---|---|---|---|
| `minimal` | `ASTER_DEBIANROOT` | 1 GiB | Direct-boot toolchain and shell checkpoint |
| `browser-web` | `ASTER_BROWSERWEB` | 3 GiB | Xorg/fbdev desktop, lxpanel/pcmanfm, Firefox, browser evidence |

`browser-web` also installs the current network evidence, PTY userspace
configuration, animation wallpaper, and the Marionette gate. The builder
rejects every other profile name; the paused `systemd-ext4`, `desktop-drm`,
Megrez, and physical-graphics profiles were removed from this line.

Run all commands from the repository root. All published images are consumed
read-only by QEMU (`snapshot=on`).

## Build

The rootfs builder requires the pinned rootfs image, which provides
`debootstrap`, `proot`, and `qemu-riscv64-static`. The explicit-QEMU path does
not touch the host's binfmt registration.

```bash
docker run --rm -it --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -e ASTERINAS_EXPLICIT_QEMU=1 \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  --profile minimal \
  --output-dir target/debian-riscv/minimal/rootfs \
  --cache-dir target/debian-riscv/cache
```

For the browser image, replace `minimal` with `browser-web`. Reuse
`target/debian-riscv/cache`; it contains the content-addressed Debian package
and metadata cache. The build verifies Debian's signed `InRelease`, checks the
resulting package closure, and publishes:

```text
debian-root.ext2
rootfs-manifest.json
packages.lock
source-metadata/InRelease
source-metadata/package-checksums
```

Verify a published image without rebuilding it:

```bash
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/browser-web/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/browser-web/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/browser-web/rootfs/packages.lock
```

The Makefile entry points are:

```bash
make build_riscv_debian_rootfs \
  DEBIAN_ROOTFS_PROFILE=browser-web \
  DEBIAN_ROOTFS_OUTPUT_DIR=target/debian-riscv/browser-web/rootfs

make build_riscv_debian_rootfs \
  DEBIAN_ROOTFS_PROFILE=minimal \
  DEBIAN_ROOTFS_OUTPUT_DIR=target/debian-riscv/minimal/rootfs
```

Optional Firefox JIT overlay packages are supplied with
`--firefox-jit-package-dir`; without them the signed Debian Firefox ESR build is
used unchanged.

## Stage1

`build_stage1.sh` builds the full root handoff. It compiles the static init,
installs the current browser/input helpers, and publishes a reproducible newc
initramfs:

```bash
docker run --rm -it \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  codex/asterinas-cross-builder:aa6f7533 \
  bash -lc 'RISC_V_CC=riscv64-linux-gnu-gcc \
    tools/riscv/debian/rootfs/build_stage1.sh \
    target/debian-riscv/stage1/initramfs.cpio'
```

The lightweight direct-boot checkpoint is built with:

```bash
make build_riscv_debian_stage1 \
  RISC_V_CC=riscv64-linux-gnu-gcc \
  DEBIAN_STAGE1_OUT=target/riscv-debian/stage1/initramfs.cpio
```

The Stage1 handoff discovers a root by label. The two accepted labels are
`ASTER_DEBIANROOT` (interactive root) and `ASTER_BROWSERWEB` (systemd/browser
root). It binds the root's `/usr/lib/asterinas` to `/run/asterinas-tools` only
for the browser root, mounts devtmpfs, devpts, tmpfs `/run` and `/tmp`, and
mounts cgroup2 before handing control to systemd or the requested root init.

Run the compiled self-tests against the current label and handoff contract:

```bash
docker run --rm -v "$PWD:/root/asterinas" -w /root/asterinas \
  codex/asterinas-cross-builder:aa6f7533 bash -lc '
    riscv64-linux-gnu-gcc -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
      -DDEBIAN_STAGE1_SELF_TEST \
      tools/riscv/debian/rootfs/stage1_init.c \
      tools/riscv/debian/rootfs/stage1_debug_console.c \
      tools/riscv/debian/rootfs/stage1_probe.c -o /tmp/st-init
    for case in systemd-browser-web-root-label systemd-tools-mount-policy \
                systemd-handoff-sequence systemd-debug-handoff-sequence; do
      qemu-riscv64-static /tmp/st-init "$case"
    done'
```

## Direct Debian gate

The direct gate is the current repeatable QEMU checkpoint. It boots the
upstream kernel with an existing toolchain rootfs as `/dev/vda`, checks `gcc`
compile/run, Python, Node, git, and (optionally) the dsh image:

```bash
make test_riscv_debian_direct \
  TARGET_ARCH=riscv64 \
  DEBIAN_DIRECT_ROOTFS=/absolute/path/to/toolchain-rootfs.ext2 \
  DEBIAN_DIRECT_DSH_IMAGE=/absolute/path/to/dsh-repo.ext2
```

Use a toolchain rootfs (the development rootfs or an image with the full
gcc/Python/Node/git set); the `minimal` profile only proves the shell and direct
boot path. The gate uses `snapshot=on` for every disk. The serial log is
`target/riscv-debian-direct.log` by default. See
[`tools/riscv/debian/README.md`](../README.md) for the environment details.

## Desktop and browser runtime

The `browser-web` image starts `asterinas-desktop.service` from `basic.target`
and runs Xorg with the `fbdev` driver against `/dev/fb0`; openbox, pcmanfm, and
lxpanel run as UID 1000. Firefox is launched by `asterinas-browser-web.service`
and the evidence service writes the Marionette/network/security records under
`/home/asterinas/browser-web-evidence`. The QEMU input contract is the fixed
`virtio-keyboard-device` then `virtio-tablet-device` pair, which appears as
`event0` (keyboard) and `event1` (pointer); the device helper deliberately does
not run evdev identity ioctls because this kernel can block on them.

The current QEMU evidence covers:

- Xorg accepting `/dev/fb0` at 1280x1024 and the desktop shell rendering;
- Firefox mapping its window and opening a Marionette session;
- virtio-net on `eth0` with the slirp fixture, DNS, and HTTPS;
- devpts/`/dev/ptmx`, `/dev/shm`, and non-root PTY allocation.

Firefox content processes now launch: the kernel no longer requires
`CAP_SYS_ADMIN` to pass a UNIX socket over a UNIX socket, so the UID-1000
browser parent can hand IPC sockets to its children. Marionette navigates to
the local owned fixture page and reads back its title and body text. A full
public-page gate remains to be run; the parity ledger records the detailed
evidence and the remaining online-navigation check.

The repository currently has no repository-level U-Boot desktop scenario. The
desktop/Firefox evidence was collected with a manual U-Boot boot using a boot
disk containing the kernel Image, this Stage1 initramfs, and `fb-bochs.dtb`
(simple-framebuffer at `0x40000000`, 1280x1024).

## Removed scope

This line intentionally contains only the QEMU-supported surface. The paused
PowerVR/DRM line, Megrez board deployment, physical-graphics gates, M2-M4
milestone gates, old host gate modules, and one-off diagnostic scripts were
removed in the cleanup commit that introduced this README. The old product tree
retains their full history; do not re-add them without a new, separately
approved workstream.
