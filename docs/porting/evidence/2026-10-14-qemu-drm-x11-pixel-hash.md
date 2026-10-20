# QEMU DRM/Xorg pixel-hash gate — 2026-10-14

This phase is QEMU-only. It does not access the EIC7700 board, write a serial
console, or perform a hardware reset.

## What passed

The current RISC-V kernel and a Debian `desktop-m4` rootfs with the virtio
sysfs compatibility links were booted with QEMU's `virtio-gpu-gl-device` and
`egl-headless` display backend. The guest evidence recorded:

```text
DRM_LIBDRM_VERSION name=virtio_gpu
DRM_LIBDRM_DEVICE PASS bustype=pci-compat transport=virtio
(II) modeset(0): glamor X acceleration enabled on virgl
(II) modeset(0): glamor initialized
```

The serial trace also contains the 3D resource-create and transfer ioctl
sequence after glamor initialization. This proves libdrm topology discovery,
EGL initialization, Mesa virgl selection, and Xorg modesetting reached the
guest DRM ioctl path.

## Screenshot observation boundary

QEMU's HMP `screendump` was deliberately attempted once and returned:

```text
Error: no surface
```

That is expected for `egl-headless`; accepting an absent image would make the
display claim unverifiable. The DRM desktop now uses the standard Debian
`xwd` client instead. `desktop_drm_x11_screenshot.py` parses the XWD header,
extracts the complete pixel area, and emits a SHA-256 over only those pixels.
The DRM classifier requires a bounded marker with 1280×800 geometry, stride,
byte count, and a 64-hex-digit pixel hash.

The first real run with `x11-apps` installed reached Xorg/virgl but the XWD
request exceeded its 10-second bound:

```text
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT_FAIL reason=xwd capture failed: ... timed out after 10.0 seconds
DEBIAN_DESKTOP_M4_FAIL reason=drm-x11-screenshot
```

The reproducible input/rootfs hash for that run is
`174540f6aacd6fa51db58c5f169407b89f3499de0ee67a2c6de75cac3b6a4aa4`; the
serial receipt hash is
`2980deb0d786c9f695e50a9ab92318911ad1177a3106fc25c4cdb4259254a556`.

This is retained as a failed screenshot receipt. It does not invalidate the
libdrm/EGL/virgl result; it identifies the remaining boundary as Xorg image
readback (or its bounded guest syscall path). The gate remains fail-closed
until one bounded XWD capture produces the pixel hash.

## ABI and readback follow-up

The initial virgl failures were traced to Debian trixie's 64-byte
`DRM_IOCTL_VIRTGPU_EXECBUFFER` ABI (`0xc0406442`). The kernel previously
accepted only the legacy 32-byte command (`0xc0206442`), which made Mesa return
`ENOTTY` on card nodes and `EACCES` on render nodes. The kernel now validates
and dispatches both layouts, while rejecting unsupported ring and sync-object
fields. It also accepts Mesa's `VIRTGPU_WAIT_NOWAIT` flag; submissions are
synchronous in this backend, so a valid handle is already idle.

A clean QEMU run with the rebuilt kernel produced the complete 3D receipt:

```text
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 \
display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
```

There were no `got error from kernel`, `ENOTTY`, or render-node `EACCES`
messages. The same run still failed the separate full-root XWD observation:

```text
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT_FAIL reason=xwd capture failed: ... timed out after 10.0 seconds
```

The trace shows Xorg's root-window readback issuing repeated
`TRANSFER_FROM_HOST_3D` and `VIRTGPU_WAIT` operations after the small EGL
probe succeeds. Therefore the remaining issue is bounded large-FBO readback,
not virgl renderer selection or command submission. The current rootfs and
boot-disk hashes for this run are respectively
`9cf2244a779ae578622e54871581c38fe48ad1bdcd86eed399931c8c025b4149` and
`1b7fd88f41c100ab6f5dc1e88a6deafd8a1ef9415758b85032646a06f5f8b315`;
the serial receipt is
`f725d90f709f1e8d0380118e59c814a19843c9b21dd211b728a2fb8827094b03`.

The next isolation run created a texture-backed 1280×800 FBO and read all
4,096,000 RGBA bytes in 82.844 ms:

```text
EGL_FULL_READBACK stage=read-after elapsed_ms=82.844 gl_error=0x0000
EGL_FULL_READBACK PASS sha256=f6cd5fac065ab3fdfc56ccb428a3a204c2361454ce9b279ac649494b620fbd68
```

The same Xorg root window completed 1×1 and 64×64 `XGetImage` calls but hung
at 1280×800. The screenshot helper now uses bounded 64×64 `XGetImage` tiles,
hashing native pixel rows in screen order. A clean guest receipt produced:

```text
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
```

This avoids treating a single multi-megabyte X11 reply as the display gate.
The browser-free DRM smoke transcript uses `CORE_CLIENTS`/`CORE_READY`; the
classifier now validates that complete ordered variant instead of requiring a
browser marker that the smoke profile intentionally disables.

The complete bounded run reached both the screenshot and recovery-ready
markers, and the host classifier returned `GateResult(passed=True,
reason='pass')`. The final input and receipt hashes were:

```text
rootfs  1df341a5473f58376f19187372e7f543e30af63b6850a1d09fa1f350e9376e11
boot    1b7fd88f41c100ab6f5dc1e88a6deafd8a1ef9415758b85032646a06f5f8b315
serial  9cf7f3d4a8ede0911db7f1ea09df1ff698bf43f21906f96cf42a6cb80262ab8f
```

## Host verification

```text
python3 -m unittest -q \
  tools.riscv.tests.test_desktop_drm_x11_screenshot \
  tools.riscv.tests.test_debian_rootfs.DebianDesktopM4GateTests \
  tools.riscv.tests.test_drm_scanout_gate \
  tools.riscv.tests.test_desktop_drm_libdrm_probe
targeted DRM/rootfs tests ... OK

python3 -m py_compile tools/riscv/debian/rootfs/desktop_drm_x11_screenshot.py \
  tools/riscv/debian/rootfs/desktop_m3_gate.py \
  tools/riscv/debian/rootfs/desktop_m4_gate.py
bash -n tools/riscv/debian/rootfs/desktop_m4_evidence.sh
git diff --check
```

The complete Debian rootfs contract suite also passed (`Ran 202 tests ...
OK`); its argument-parser usage line is expected because the suite exercises
the command-line parser in subprocesses.

The separate scanout classifier still injects missing, reordered, duplicated,
and failed `SETCRTC`, transfer, and flush traces. A failed attempt clears the
old result before a clean retry, so stale success cannot survive recovery.
