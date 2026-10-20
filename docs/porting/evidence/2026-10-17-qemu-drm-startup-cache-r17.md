# QEMU DRM startup-cache replay: r17

Date: 2026-10-17 (Asia/Shanghai)

## Change under test

The rootfs builder now treats `desktop-m4` as a startup-cache profile and the
cache checker accepts it. The generated image contains precomputed sysusers,
ld.so, journal catalog, and fontconfig data plus `/etc/.updated` and
`/var/.updated`. The source tests and shell/Python syntax checks passed.

The image was built independently at:

`target/debian-riscv/desktop-m4-cached-20261017-r2/rootfs`

The build completed all eight phases, including signed package/index
verification and ext2 verification.

## Runtime replay

The GL QEMU container was run with `/dev/dri/renderD128` mapped and the new
image. The gate reached:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
```

Within the bounded 60-second diagnostic boot window it emitted no Debian
systemd, Xorg, virgl, pixel, or recovery marker. The machine-readable result
is:

`/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r17/result.json`

with `passed=false` and `reason=protocol`; the serial receipt is in the same
directory. A longer r16 replay was stopped at the external 240-second safety
boundary before publication, so r17 is the authoritative bounded receipt.

## Interpretation

Precomputing first-boot caches removes the repeated sysusers/ldconfig work from
the rootfs build, but it does not yet prove a working Asterinas-to-systemd
handoff. Task 5 remains open: there is still no runtime `renderer=virgl`,
pixel hash, or clean recovery receipt. The next investigation is the
systemd exec/early userspace ABI boundary (console, proc/sysfs and device
handoff) using a smaller root-init diagnostic; no physical display experiment
is scheduled until that gate closes.

