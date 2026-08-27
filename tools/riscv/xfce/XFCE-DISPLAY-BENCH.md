# RISC-V Xfce display-path benchmark and Debian compatibility

Date: 2026-08-27

## Result

Asterinas has two kinds of fbdev-versus-DRM measurements:

1. the existing M14 `xbench` primitive-operation benchmark; and
2. a new controlled Xfce cold-boot comparison using the same kernel, base root, Debian runtime closure, CPU count, memory, and 1280x800 display size, with display-specific Xorg configuration.

The hardened cold-boot sample reached the complete Xfce gate in 2min 8.415s on firmware fbdev and 2min 26.517s on DRM modesetting with a software shadow framebuffer.
DRM was 18.102s, or 14.1%, slower in this sample.
This is an initial TCG result with one run per path, not a stable performance claim.

## Existing operation benchmark

[`DRM-M14-report.md`](../nixos/DRM-M14-report.md) already compares an X11 micro-benchmark on bochs fbdev and virtio-gpu modesetting.
It covers fullscreen fills, synchronized and batched rectangles, lines, points, and 64x64 image uploads.
The paired single-vCPU sample favored fbdev for most operations, while earlier samples reversed several rankings.
Both paths were software-rendered and varied by 8-14x with host contention, so M14 correctly treats the absolute numbers as noisy rather than declaring either driver universally faster.

The stable observation was that image upload was pathological and identical on both drivers at 6 operations per second.
That bottleneck was in the X server's software image handling rather than DRM.

## Controlled cold-boot comparison

### Method

Both runs used:

- Asterinas RISC-V kernel source at `4fae823d1e062bb2ddf7b3da00396cf47220ad67`, on `codex/drm-main-sync`;
- kernel image SHA-256 `532439bed2ea3f475b2d789103716e8417639ce7e516741dc9a54a41c4cea5a8`;
- the same persistent Xfce base and Debian Trixie riscv64 Xorg runtime;
- QEMU TCG with four vCPUs and 2 GiB RAM;
- a 1280x800 XRGB framebuffer;
- the same Xorg/Xfce services and serial acceptance markers.

Only the display path changed:

| Path | QEMU device | Guest driver | Acceleration |
|---|---|---|---|
| no DRM | `bochs-display` plus firmware `simple-framebuffer` | Xorg `fbdev` on `/dev/fb0` | software shadow framebuffer |
| DRM | `virtio-gpu-pci` | Xorg `modesetting` on `/dev/dri/card0` | software `ShadowFB`; glamor disabled |

The no-DRM path does not instantiate a virtio-gpu device.
U-Boot adds a fixed-width `simple-framebuffer` node for bochs at boot.
The acceptance gate requires `fbdev_drv.so`, `FBDEV(0): using /dev/fb0`, Xorg virtual size 1280x800, and substantial nonblack content spanning both dimensions of the guest region.
The bochs host surface remains 1280x1024, so the visual check deliberately ignores its untouched rows below the 1280x800 guest framebuffer.
The DRM gate requires `modesetting_drv.so` plus `modeset(0): using default device`.

### Timing

| Path | Kernel | Userspace | Total |
|---|---:|---:|---:|
| no DRM / fbdev | 11.804s | 1min 56.610s | 2min 8.415s |
| DRM / software modesetting | 16.433s | 2min 10.083s | 2min 26.517s |
| DRM delta | +4.629s | +13.473s | **+18.102s (+14.1%)** |

Both the kernel and userspace phases were shorter on fbdev in this sample.
Startup is dominated by emulated RISC-V userspace rather than graphics initialization, so more alternating runs on an otherwise idle host are required before treating the size of this difference as reproducible.
Three earlier 1280x800 fbdev boots reached the same serial milestone in 2min 15.748s, 2min 30.061s, and 3min 3.915s.
That spread is much larger than the accepted A/B delta and reinforces that this cold-boot sample does not establish a significant startup advantage.

The serial evidence is kept outside Git under `target/xfce-display-bench/`:

| Artifact | SHA-256 |
|---|---|
| `drm-software.serial.log` | `f3d18df96f71fce6f8f5ba7cdee997430a1930e9207385e06a9c5f56d63095cd` |
| `fbdev-1280x800-hardened.serial.log` | `f8e9e612efa755c0c5b1c8b2e36ad8a66214f814042c2c0f1f0b7486b81bbb52` |
| `drm-software-root.ext2` | `82f366eff6c3265318e1da3308187f3283754a60beb170c2a6a9409de6de553f` |
| `fbdev-1280x800-final-root.ext2` | `45d6e25c2ba42a06a96a53ca837e7b3d13d9cb0b8ddae267c39e87e66f0d4ae2` |
| `fbdev-1280x800-hardened.ppm` | `2a9d840a3f30d4622c98687982d5de19a7bf7fc7d3a1139189276cfeabf0af87` |
| `target/drm-m19/u-boot` | `5b737e8c6b7cecd80f0d15872b707592592921668cc9757796205a364661bdba` |
| `target/drm-m19/qemu-virt.dtb` | `34fe143c0783dd1444fc280cd8218d15ef4bb0b28ce34e4517f333343d776863` |

### Reproduce

Build the Xfce base described in [`XFCE-M3-report.md`](XFCE-M3-report.md) and fetch the Debian M19 runtime described in [`XFCE-DRM-M1-report.md`](XFCE-DRM-M1-report.md):

```sh
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
printf '%s  %s\n' \
    532439bed2ea3f475b2d789103716e8417639ce7e516741dc9a54a41c4cea5a8 \
    target/osdk/aster-kernel-osdk-bin.Image | sha256sum --check -

# Build the pinned U-Boot tool and generate a four-hart, 2-GiB DTB.
python3 tools/riscv/make_qemu_uboot_initramfs.py \
    target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_uboot_booti \
    ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel-osdk-bin.Image" \
    ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
mkdir -p target/drm-m19
cp target/qemu-uboot/cache/u-boot-build/u-boot target/drm-m19/u-boot
qemu-system-riscv64 \
    -machine virt,dumpdtb=target/drm-m19/qemu-virt.dtb \
    -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
    -m 2G -smp 4 -nographic

bash tools/riscv/xfce/build_xfce_deps.sh
bash tools/riscv/xfce/build_xfce_libs.sh
bash tools/riscv/xfce/build_xfce_apps.sh
bash tools/riscv/xfce/pack_xfce_initramfs.sh
bash tools/riscv/nixos/m19/fetch_debian_rootfs.sh

# DRM with CPU rendering
python3 tools/riscv/xfce/build_xfce_drm.py \
    --base target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio \
    --runtime target/m19/rootfs \
    --software-display
python3 tools/riscv/xfce/boot_xfce_drm.py --software-display

# No DRM: bochs firmware framebuffer and Xorg fbdev
python3 tools/riscv/xfce/build_xfce_drm.py \
    --base target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio \
    --runtime target/m19/rootfs \
    --fbdev-display
python3 tools/riscv/xfce/boot_xfce_drm.py --fbdev-display
```

The explicit `--base` avoids the launcher's historical sibling-worktree default and makes the input location visible in the command.

## Debian compatibility

The DRM implementation already supports Debian user space.
The M19 runtime used by the DRM, virgl, and Xfce gates consists of Debian Trixie riscv64 binaries.
Debian's Xorg modesetting driver opens Asterinas `/dev/dri/card0`, and the M19
raw-EGL gate exercises Debian Mesa's virgl driver.
The software-DRM comparison above confirms the Xorg/Xfce integration but does
not itself prove application-side GPU rendering. That direct GLX/DRI3 gate is
recorded separately in [`XFCE-DRM-M2-report.md`](XFCE-DRM-M2-report.md).

The official Asterinas Debian desktop pipeline has not adopted DRM yet.
Its `desktop-m3` and `desktop-m4` profiles explicitly install `xserver-xorg-video-fbdev`.
The QEMU gate starts `bochs-display`.
Its acceptance marker requires `framebuffer=fbdev`.
Therefore the precise status is:

- **Debian ABI and graphics runtime on Asterinas DRM: supported and tested.**
- **The official Debian desktop profile/gate on virtio-gpu DRM: not yet integrated.**

Integrating it should reuse the current Debian profile and systemd work while adding the Mesa/GBM/DRM runtime closure.
It should then select Xorg modesetting, replace bochs with virtio-gpu in the graphical gate, and assert DRM/virgl markers.
The fbdev profile should remain available as a fallback and A/B baseline.
