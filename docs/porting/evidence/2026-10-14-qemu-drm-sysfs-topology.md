# QEMU DRM sysfs topology phase — 2026-10-14

This is a bounded QEMU-only phase. It does not access the EIC7700 board and
does not prove native PowerVR acceleration.

## Change

`kernel/src/device/sysfs.rs` now builds a read-only DRM topology after the
DRM character nodes are registered:

```text
/sys/class/drm/card0                 -> ../../devices/virtual/virtio/virtio0/drm/card0
/sys/class/drm/renderD128            -> ../../devices/virtual/virtio/virtio0/drm/renderD128
/sys/dev/char/226:0                   -> ../../devices/virtual/virtio/virtio0/drm/card0
/sys/dev/char/226:128                 -> ../../devices/virtual/virtio/virtio0/drm/renderD128
/sys/devices/virtual/virtio/virtio0/drm/{card0,renderD128}
```

The node attributes include Linux-compatible `dev` and `uevent` values. The
virtual parent exposes `driver`, `subsystem`, `PCI_SLOT_NAME`, vendor/device,
subsystem IDs, revision and `modalias` values. The PCI-shaped fields are a
userspace compatibility description for libdrm's virtio path; they are not a
claim that the EIC7700 board has PCI hardware. Firmware-only `simpledrm` boots
do not get this virtio topology.

## Verification

The normal RISC-V kernel build passed after the change:

```text
tools/docker/run_dev_container.sh -- make kernel \
  TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

The relevant Python and shell gates passed:

```text
python3 -m unittest -q \
  tools.riscv.tests.test_debian_rootfs.DebianDesktopM4GateTests \
  tools.riscv.tests.test_debian_desktop_m4_x11_gate \
  tools.riscv.tests.test_drm_render_node_gate \
  tools.riscv.tests.test_drm_virgl_param_gate
Ran 48 tests in 0.807s
OK

git diff --check
bash -n tools/riscv/debian/rootfs/desktop_m3_device_access.sh \
  tools/riscv/debian/rootfs/desktop_m4_evidence.sh \
  tools/riscv/debian/rootfs/desktop_m5_session.sh
```

The broader browser, QEMU contract, scanout, modesetting, render-node, GEM and
virgl unit set also passed: `Ran 203 tests ... OK`.

The guest diagnostic in the GL QEMU desktop image observed both real DRM
character nodes and the new attributes:

```text
ASTERINAS_DEV path=/dev/dri/card0 stat=character special file e2:0 root:root:600
ASTERINAS_DEV path=/dev/dri/renderD128 stat=character special file e2:80 root:root:600
ASTERINAS_SYSFS_FILE path=/sys/devices/virtual/virtio/virtio0/uevent value=DRIVER=virtio_gpu|PCI_SLOT_NAME=0000:00:00.0|MODALIAS=virtio:d00000001v00001AF4|
ASTERINAS_RESOLVE_FILE path=/sys/dev/char/226:0/device/vendor value=0x1af4|
ASTERINAS_RESOLVE_FILE path=/sys/dev/char/226:0/device/device value=0x1050|
```

## Boundary

The accelerated desktop gate is still **not passed**. The current GL QEMU
launch reaches Xorg with the DRM provider and the synthetic PCI-compatible
sysfs fields, but the run has not yet produced an Xorg `glamor + virgl`
renderer marker, an EGL/GLES pixel hash, or a Firefox WebRender receipt. Some
runs stop while Xorg is in Mesa/libdrm discovery; that is retained as a
failure receipt rather than being interpreted as acceleration.

The next bounded phase is to make the canonical-link resolution and
`drmGetDevice2` result observable in-guest, then fix only the missing topology
or DRM ioctl contract identified by that receipt. No board MMIO, serial write,
hardware reboot, or automatic retry is part of this phase.

The desktop evidence script now has a fail-closed `drm_sysfs_ready` check and
emits `DEBIAN_DESKTOP_M4_DRM_SYSFS` before the DRM node and Xorg markers. Thus
an Xorg screenshot cannot be accepted merely because `/dev/dri/*` exists while
the Mesa discovery topology is incomplete.

The M4 rootfs builder also installs `desktop_drm_libdrm_probe.py`. Its public
`drmVersion` ABI uses `int` length fields (the kernel ioctl structure uses
`size_t`); this distinction is covered by a host unit test. The old ctypes
layout was reproduced as a SIGSEGV at address `0x8` and is fixed. The first
guest run, before the parent `drm/subsystem` link was added, recorded:

```text
DRM_LIBDRM_VERSION name=virtio_gpu
DRM_LIBDRM_DEVICE SKIP bustype=virtio reason=libdrm-enodev
```

That result is retained as historical evidence only. The probe now treats
`ENODEV` as a failure: the parent `drm/subsystem` link is required so the
kernel topology reaches libdrm's virtio parser. That parser uses its PCI
compatibility path for virtio-mmio and reports `DRM_BUS_PCI` (0); the probe
accepts that value only with an explicit `transport=virtio` marker (and also
accepts `DRM_BUS_VIRTIO` 0x10 from newer libdrm). A fresh guest run is still
required before this phase can be called complete.

With the fixed probe, QEMU no longer sees the earlier guest SIGSEGV. Xorg
reaches modesetting and loads `glamoregl`, but the current runtime receipt is:

```text
(EE) modeset(0): eglInitialize() failed
(II) modeset(0): glamor initialization failed
DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout
```

The software `AccelMethod=none` control run reaches the virtual 1280x800 KMS
mode; thus the remaining blocker is confined to EGL/virgl/libdrm integration,
not scanout or basic modesetting. No board MMIO, serial write, hardware reboot,
or physical experiment was performed.
