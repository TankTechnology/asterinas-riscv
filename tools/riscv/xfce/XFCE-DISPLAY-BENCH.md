# RISC-V Xfce display-path benchmark and Debian compatibility

Date: 2026-08-27

## Result

Asterinas has two kinds of fbdev-versus-DRM measurements:

1. the existing M14 `xbench` primitive-operation benchmark; and
2. a new controlled Xfce cold-boot comparison using the same kernel, rootfs,
   CPU count, memory, and 1280x800 display size.

The new cold-boot sample reached the complete Xfce acceptance gate in
3min 3.915s on firmware fbdev and 2min 26.517s on DRM modesetting with a
software shadow framebuffer. DRM was 37.398s, or 20.3%, faster in this sample.
This is an initial TCG result with one run per path, not a stable performance
claim.

## Existing operation benchmark

[`DRM-M14-report.md`](../nixos/DRM-M14-report.md) already compares an X11
micro-benchmark on bochs fbdev and virtio-gpu modesetting. It covers fullscreen
fills, synchronized and batched rectangles, lines, points, and 64x64 image
uploads. The paired single-vCPU sample favored fbdev for most operations, while
earlier samples reversed several rankings. Both paths were software-rendered
and varied by 8-14x with host contention, so M14 correctly treats the absolute
numbers as noisy rather than declaring either driver universally faster.

The stable observation was that image upload was pathological and identical on
both drivers at 6 operations per second. That bottleneck was in the X server's
software image handling rather than DRM.

## Controlled cold-boot comparison

### Method

Both runs used:

- the current `codex/drm-main-sync` Asterinas RISC-V kernel;
- the same persistent Xfce base and Debian Trixie riscv64 Xorg runtime;
- QEMU TCG with four vCPUs and 2 GiB RAM;
- a 1280x800 XRGB framebuffer;
- the same Xorg/Xfce services and serial acceptance markers.

Only the display path changed:

| Path | QEMU device | Guest driver | Acceleration |
|---|---|---|---|
| no DRM | `bochs-display` plus firmware `simple-framebuffer` | Xorg `fbdev` on `/dev/fb0` | software shadow framebuffer |
| DRM | `virtio-gpu-pci` | Xorg `modesetting` on `/dev/dri/card0` | software `ShadowFB`; glamor disabled |

The no-DRM path does not instantiate a virtio-gpu device. U-Boot adds a
fixed-width `simple-framebuffer` node for bochs at boot, and the gate requires
`fbdev_drv.so` plus `FBDEV(0): using /dev/fb0`. The DRM gate requires
`modesetting_drv.so` plus `modeset(0): using default device`.

### Timing

| Path | Kernel | Userspace | Total |
|---|---:|---:|---:|
| no DRM / fbdev | 14.690s | 2min 49.224s | 3min 3.915s |
| DRM / software modesetting | 16.433s | 2min 10.083s | 2min 26.517s |
| DRM delta | +1.743s | -39.141s | **-37.398s (-20.3%)** |

The kernel phase was slightly shorter on fbdev, but the DRM run reached the
complete userspace desktop gate much sooner. Startup is dominated by emulated
RISC-V userspace rather than graphics initialization, so more alternating
runs on an otherwise idle host are required before treating the size of this
difference as reproducible.

The serial evidence is kept outside Git under `target/xfce-display-bench/`:

| Artifact | SHA-256 |
|---|---|
| `drm-software.serial.log` | `f3d18df96f71fce6f8f5ba7cdee997430a1930e9207385e06a9c5f56d63095cd` |
| `fbdev-1280x800.serial.log` | `f109f1c4ab5e7d88c3264c82163a7f20e962e2205421c79f9ca1b81d503a6ed5` |
| `drm-software-root.ext2` | `82f366eff6c3265318e1da3308187f3283754a60beb170c2a6a9409de6de553f` |
| `fbdev-1280x800-root.ext2` | `5aa244dfecd70ac321ca10fd0460ac2b9a331b02ca5fc226dca6224587fb7189` |

### Reproduce

After preparing the Xfce base and Debian M19 runtime described in
[`XFCE-DRM-M1-report.md`](XFCE-DRM-M1-report.md):

```sh
# DRM with CPU rendering
python3 tools/riscv/xfce/build_xfce_drm.py --software-display
python3 tools/riscv/xfce/boot_xfce_drm.py --software-display

# No DRM: bochs firmware framebuffer and Xorg fbdev
python3 tools/riscv/xfce/build_xfce_drm.py --fbdev-display
python3 tools/riscv/xfce/boot_xfce_drm.py --fbdev-display
```

## Debian compatibility

The DRM implementation already supports Debian user space. The M19 runtime
used by the DRM, virgl, and Xfce gates consists of Debian Trixie riscv64
binaries. Debian's Xorg modesetting driver opens Asterinas
`/dev/dri/card0`, and Debian Mesa's virgl driver has completed the EGL/render
gate. The controlled DRM Xfce run above is another full-system Debian-runtime
confirmation.

The official Asterinas Debian desktop pipeline has not adopted DRM yet. Its
`desktop-m3` and `desktop-m4` profiles explicitly install
`xserver-xorg-video-fbdev`; the QEMU gate starts `bochs-display`; and its
acceptance marker requires `framebuffer=fbdev`. Therefore the precise status
is:

- **Debian ABI and graphics runtime on Asterinas DRM: supported and tested.**
- **The official Debian desktop profile/gate on virtio-gpu DRM: not yet
  integrated.**

Integrating it should reuse the current Debian profile and systemd work while
adding the Mesa/GBM/DRM runtime closure, selecting Xorg modesetting, replacing
bochs with virtio-gpu in the graphical gate, and asserting DRM/virgl markers.
The fbdev profile should remain available as a fallback and A/B baseline.
