# QEMU Firefox DRM scene r51

Date: 2026-10-17 (Asia/Shanghai)

## Result

**FAIL CLOSED.** This run used the current repository Firefox DRM scene gate,
scene document, and service in a disposable copy of the r47 fallback image.
The only runtime variation was:

```text
Environment=MESA_LOADER_DRIVER_OVERRIDE=virpipe
Environment=GALLIUM_DRIVER=virpipe
```

The gate proved render-node permissions, then the guest EGL probe lost its
rendering-server connection and the Firefox Marionette endpoint became
unreachable:

```text
FIREFOX_DRM_ACCESS groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_EGL_PROBE rc=-6 bytes=52 text=lost connection to rendering server on 8 read -1 22
FIREFOX_DRM_GLXTEST rc=-6 bytes=0 text=none
...
A_WEB_MARIONETTE_TRANSPORT ... error_type=ConnectionRefusedError
```

The QEMU process was stopped immediately after this fail-closed boundary. No
WebRender renderer, screenshot hash, frame samples, or restart receipt was
accepted. The result rejects the `virpipe` environment-only approach and does
not indicate a physical-board result.

## Evidence

- Serial log: `r51/serial.log`
- Serial SHA-256: `ef457c875ed8d098dbb0e83d654443913d4c8f0d9215605f7e21d25bebcef835`
- Temporary image SHA-256 before execution:
  `72042767343db7815188146e030611d854913739da28bebb41b549ccea791c53`
- QEMU: `asterinas/qemu-gl-env:20261014`, `virtio-gpu-gl-device`,
  `egl-headless`, `-nic none`
- Physical board: not used
