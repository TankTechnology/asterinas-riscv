# QEMU virtio-gpu and DRM audit, 2026-10-06

This is a bounded QEMU-only device audit using the same release kernel and
Debian root image as the local Firefox canary. It does not start the physical
board and does not claim PowerVR or hardware acceleration.

QEMU was started with `virtio-gpu-device` instead of `bochs-display`, with no
simple-framebuffer node injected. After the root debug console reported UID 0,
the probe inspected the DRM and framebuffer nodes.

## Observed device contract

```text
virtio: virtio-gpu: virgl 3D unavailable
virtio: virtio-gpu: 1 scanout(s), primary 1280x800
virtio: virtio-gpu: RESOURCE_CREATE_2D ok
virtio: virtio-gpu: ATTACH_BACKING ok
virtio: virtio-gpu: SET_SCANOUT ok
virtio: virtio-gpu: TRANSFER_TO_HOST_2D ok
virtio: virtio-gpu: FLUSH ok
virtio: virtio-gpu: presented 1280x800 test pattern on scanout 0

/dev/dri/card0
/dev/dri/renderD128
/dev/fb0: No such file or directory
```

This separates the layers clearly:

- QEMU virtio-gpu is registered and its 2D command path reaches the host
  scanout.
- Asterinas publishes DRM card and render nodes for that device.
- This run has no fbdev node, so the existing fbdev Xorg desktop cannot be
  treated as a virtio-gpu desktop without switching to the DRM desktop
  profile and modesetting Xorg configuration.
- Virgl/3D is unavailable in this QEMU invocation; Firefox remains software
  rendered.

The full bounded serial transcript is retained outside the repository at
`/tmp/asterinas-browser-web-offline-overlay-20261005/qemu-virtio-gpu-probe.serial.log`.

The next QEMU graphics step is therefore the existing `desktop-drm` profile:
build or locate a schema-eight DRM rootfs, run its `virtio-gpu-device` gate,
and require the ordered DRM/Xorg markers plus a non-empty pixel capture. A
virgl run should remain a separate experiment and must report the renderer
explicitly rather than being inferred from the presence of `/dev/dri`.
