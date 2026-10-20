# QEMU Firefox DRM/WebRender scene — 2026-10-14

This is a QEMU-only receipt. It does not access the development board, write a
serial console, or perform a hardware reset.

## Rootfs and host preparation

The `browser-web` rootfs initially failed during package admission because an
old cached `at-spi2-common` archive was hashed before the installed-package
filter. The builder now reads package name, architecture, and version first;
superseded archives are skipped, while installed archives still require a
matching authenticated Packages index and SHA-256. Regression coverage passes
for both cases.

The profile also briefly requested a nonexistent
`xserver-xorg-video-modesetting` package. Debian's modesetting driver is
provided by `xserver-xorg-core`; the invalid package was removed. The signed
browser rootfs then built successfully with Firefox ESR, libdrm, EGL, Mesa
DRI, GLES, and X11 utilities.

The runtime attempt used a disposable copy of that image with the scene
service and gate injected through `debugfs`; that injection intentionally
invalidates the published manifest and is not a release artifact.

```text
rootfs  b080eee5b724631ef75ce0d31f59e991dc7d5f4aa9a54ce069cb075273158714
manifest 049e321850f7fc2890c62dc4a1e0560dd941d58e1529614628086404080fec19
```

A minimal QEMU 10.2.1 was built inside the development container with
`riscv64-softmmu`, EGL/OpenGL, and virglrenderer enabled. Its preflight reports
`egl-headless` and `virtio-gpu-gl-device`.

## Fixed Firefox scene gate

The browser image now contains a separate local scene and gate. It writes a
bounded profile, starts Firefox on the DRM desktop, connects to Marionette,
checks `about:support`, runs a 32-frame canvas/transform workload, captures a
PNG, emits a screenshot digest and frame quantiles, and restarts Firefox for a
recovery receipt. The host parser requires exactly one ordered renderer,
screenshot, frame, and recovery record; it rejects unavailable WebRender,
llvmpipe/softpipe, bad geometry, duplicate receipts, unordered quantiles, and
short frame samples.

Host parser and rootfs/browser contract tests pass (`218` tests in the focused
run; the command-line usage line is expected from the rootfs parser tests).

## Runtime result

The DRM desktop itself reaches Xorg modesetting/glamor and virgl before the
Firefox service starts. Firefox reaches the Marionette TCP greeting,
`WebDriver:NewSession`, and `SetWindowRect`. The remaining browser boundary is
Firefox ESR 153's privileged `about:support` page:

* `WebDriver:Navigate` rejects `about:support` with
  `unsupported operation`;
* a script location assignment is also rejected;
* a command-line `about:support` startup produces a dynamic page source without
  the rendered graphics values; and
* content-script execution on that privileged page is rejected; enabling
  Firefox's explicit `--remote-allow-system-access` diagnostic mode still
  yields only the dynamic page skeleton, with no usable renderer values.

The Firefox process remains alive; its captured diagnostic is a nonfatal VA-API
probe failure. No kernel panic, QEMU exit, DRM ioctl failure, or Xorg renderer
failure appears in the receipt.

The latest bounded rerun (after adding per-command Marionette deadlines) is:

```text
serial e375a9b0bc758ee25d2d2c5ae22bbfc176c0d551ca109d8434be11a3c9cfbe98
```

That run failed closed at the bounded `WebDriver:NewSession` call after the
Marionette greeting; the Firefox log contained only the nonfatal DBus/VA-API
startup diagnostics. An earlier run reached `SetWindowRect` and the
`about:support` phase but returned only a 15-byte dynamic skeleton. These are
two separate Firefox-side gates: startup latency under RISC-V emulation and
the privileged graphics-report interface. Neither is a DRM/Xorg/virgl kernel
failure, and neither is evidence of a completed Firefox hardware-acceleration
path.
