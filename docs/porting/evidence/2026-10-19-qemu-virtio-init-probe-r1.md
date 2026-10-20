# QEMU virtio-gpu initialization probe r1

## Result

This was a bounded 120-second probe with `virtio-gpu-gl-device` and the
Firefox probe rootfs.  The new kernel-stage markers show that the previous
`Starting kernel ...` boundary did not reproduce:

```text
VIRTIO_INIT stage=enter
VIRTIO_INIT stage=transport-ready
VIRTIO_INIT stage=device type=Gpu
VIRTIO_INIT stage=features type=Gpu
VIRTIO_GPU_INIT stage=enter
VIRTIO_GPU_INIT stage=features virgl=true scanouts=1
VIRTIO_GPU_INIT stage=queues-ready
VIRTIO_GPU_INIT stage=device-ready
VIRTIO_GPU_INIT stage=display-query-done width=1280 height=800
VIRTIO_GPU_INIT stage=framebuffer-ready
VIRTIO_GPU_INIT stage=registered
VIRTIO_GPU_INIT stage=pattern-done
VIRTIO_INIT stage=device-result type=Gpu ok=true
ASTERINAS_BOOT_PHASE phase=init-spawned
```

The guest reached the DRM render node and userspace.  Firefox then spent the
remaining short budget before Marionette greeting: `glxtest` timed out at its
20-second bound, followed by probe profile/spawn/connect markers and repeated
`ConnectionRefusedError` while Firefox was still cold-starting.  No
`NewSession` request was sent, so this run is not a new NewSession result.

## Interpretation

The early GL/virtio-gpu launch boundary is open for this kernel.  The next
probe should retain the same inputs but allow the independent Firefox short
probe enough time to reach greeting/NewSession; the 15-minute scene gate is
still frozen.

## Inputs and artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `616e4671b293e367b63706acac5815f3cf7d1601ae64b7e4a821dd6be4ff472b` |
| `result.json` | `857960c35d5554721e880d5a8bbaf07c3ad2aad636dae9113f929cb900b711dd` |
| `lifecycle.json` | `ce96df7d1df1309421ce7ac5027fdc8909ab4142224758f5b3d3844c412f74f0` |
| kernel | `36279042694a2538c9f31a99d803f5108d8da0fa06b19d91510868a1564fd549` |
| QEMU | `d4a95ece1ea69687257cb584182bce231d8f379fda0231ec62d4da07611e1952` |
| rootfs | `df7e8dcaee262cf1966eb4f0fbf6ad745d8ef20ea4f5a53e1faaaa77a69326ca` |

Output group: `/mnt/mydisk/qemu-virtio-init-probe-r1/`
