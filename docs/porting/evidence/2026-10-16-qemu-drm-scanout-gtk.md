# QEMU DRM scanout through GTK host backend

Date: 2026-10-16 (Asia/Shanghai)

## Scope

This is a QEMU-only host-display receipt. The guest is the existing
`drm-current-scanout-20261015` artifact; no physical board, serial device, or
hardware MMIO was used. QEMU 10.2.1 ran with `-display gtk` against an isolated
Xvfb server. The registered device set is `drm-scanout-gtk`, which keeps the
same `virtio-gpu-device` and DRM scanout trace contract as the headless gate.

The GTK surface reports the guest's selected 640x480 scanout. This is why the
GTK contract has a separate 640x480 capture geometry; the headless QMP path
retains its 1280x800 virtual canvas and is not silently re-labelled.

## Gate

```text
tools/riscv/drm/scanout_gate.py --device-set drm-scanout-gtk
```

Result: **PASS**

The QEMU argv contains:

```text
-display gtk ... -device virtio-gpu-device
-trace enable=virtio_gpu_cmd_set_scanout
-trace enable=virtio_gpu_cmd_res_xfer_toh_2d
-trace enable=virtio_gpu_cmd_res_flush
```

The serial receipt contains the ordered guest markers and exactly five host
control traces: resource create, backing attach, SET_SCANOUT, transfer to host,
and RESOURCE_FLUSH. The screenshot audit reports 640x480, a full non-black
bounding box, 307180 non-black pixels, and at least three distinct colors.

## Reproducible evidence

| Artifact | Path | SHA-256 |
| --- | --- | --- |
| Result | `target/qemu-uboot/drm-current-scanout-gtk-20261016/gtk-evidence/result.json` | `384cbd081cae6c7981809ef00f5434591ef3eb73b112b094c840ef2e64b72bf2` |
| Serial | `target/qemu-uboot/drm-current-scanout-gtk-20261016/gtk-evidence/serial.log` | `68c89efc256201afc0cb018c34a043ff1be1ce3b93cd25d2d5e773f7852a8fac` |
| QMP PPM | `target/qemu-uboot/drm-current-scanout-gtk-20261016/gtk-evidence/scanout.ppm` | `2f90fdc98b17ea03e030efc6fed9a5028f2fab66c05d3577ee75161ce5c2f33a` |
| Display audit | `target/qemu-uboot/drm-current-scanout-gtk-20261016/gtk-evidence/display-audit.json` | `2f246e536603c3999194a3945ecf6623fbab4431277fbffe00efdcf4eb992bbc` |

The PPM pixel payload hash is
`2f90fdc98b17ea03e030efc6fed9a5028f2fab66c05d3577ee75161ce5c2f33a`.
The PNG preview is retained next to the PPM for visual inspection; the PPM
and its pixel hash are the canonical gate artifacts.

The host backend was rerun after the profile and fault-gate restoration. The
fresh receipt is under
`target/qemu-uboot/drm-current-scanout-gtk-20261016/gtk-evidence-rerun/`.
It produced the same result hash
`384cbd081cae6c7981809ef00f5434591ef3eb73b112b094c840ef2e64b72bf2`, the
same PPM pixel hash above, and a fresh serial hash
`69bcc24b5a306aab5a271ec4883b989705003a717938ee5abc23323cdf13280c`.

This closes the host GTK scanout backend. It does not close Task 5's real
context/submit/fence fault injection and does not claim Firefox WebRender or
physical-board GPU acceleration.
