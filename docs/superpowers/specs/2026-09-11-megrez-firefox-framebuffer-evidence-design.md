# Megrez Firefox framebuffer evidence design

## Goal

Make the unattended physical Firefox homepage transaction repeatable without
allowing Firefox's intermittent `WebDriver:TakeScreenshot` response to obscure
an otherwise complete browser result.  The transaction must still retain a
real image of the board's displayed output and must not weaken the existing
URL, TLS, DOM, Firefox identity, graphical readiness, or U-Boot recovery
requirements.

## Evidence behind the change

Two consecutive runs with the same kernel, root image, DTB, Stage1, Firefox,
and Baidu contract both completed navigation and the validated DOM snapshot.
In the first run, `WebDriver:TakeScreenshot` sent request 14 and returned no
response header before the bounded gate expired.  In the second run, the same
command returned a complete 1280-by-887 PNG after 22.57 seconds and the whole
transaction passed.  This localizes the variable boundary to the Firefox
screenshot actor or its IPC/wakeup path; it is not evidence that DNS, TCP,
TLS, page navigation, JavaScript, or the visible desktop failed.

The current kernel already implements bounded reads from `/dev/fb0`, and the
frozen Debian root already supplies Python.  No package install, root-image
rewrite, or kernel rebuild is needed.

## Considered approaches

1. Retrying `WebDriver:TakeScreenshot` was rejected.  A stuck Marionette
   request leaves the session unusable and another retry would consume most of
   a physical boot without identifying a new boundary.
2. `xwd` followed by a host conversion would avoid the Firefox actor, but it
   adds an X11 request, a second artifact format, and a host converter to the
   acceptance chain.
3. Direct framebuffer capture is selected.  It reads the exact scanout memory
   exposed by Asterinas after the DOM is known ready and emits a bounded PNG
   using the Python standard library.

## Guest contract

The existing Marionette gate gains a `--screenshot-backend` option.  Its
default remains `marionette`; this preserves the full browser-web gate and all
QEMU contracts.  The physical `baidu-home` transaction explicitly selects
`framebuffer`.

For the framebuffer backend, the gate writes and validates the Baidu JSON,
closes the Marionette transport, and only then captures `/dev/fb0`.  The
capture reads Linux `FBIOGET_VSCREENINFO` and `FBIOGET_FSCREENINFO`, accepts
only the current true-color 32-bit BGR-reserved layout, validates all size and
stride arithmetic, reads exactly the visible scanout, and encodes an
8-bit RGB, non-interlaced PNG.  It rejects uniform or insufficiently diverse
sampled pixels, an oversized raw buffer, an oversized PNG, short reads,
unsupported offsets/layouts, and unsafe output paths.

The final homepage marker is emitted only after both the DOM and framebuffer
capture complete.  It records `screenshot_source=framebuffer`, PNG SHA-256,
width, and height.  The host checks those fields against the transferred PNG.

## Host lifecycle and failure handling

`RealFirefoxBrowseOperations` selects the framebuffer backend only for its
lightweight physical homepage command.  The existing serial command-size,
deadline, artifact-transfer, diagnostic, reboot, and recovery rules remain in
force.  A framebuffer failure preserves the already-written DOM JSON in the
bounded diagnostic bundle and produces a failing result.

The result still publishes `baidu-home.png`; its dimensions are the physical
1920-by-1080 framebuffer rather than Firefox's 1280-by-887 content viewport.
This is labelled as framebuffer evidence and must never be described as an
external HDMI capture.

## Verification

Unit tests must prove BGR-reserved conversion including padded rows, PNG
structure and nonblank rejection, strict framebuffer metadata validation,
capture ordering after Marionette close, CLI default compatibility, explicit
physical backend selection, marker-to-PNG hash/dimension binding, and unchanged
failure recovery.  The affected browser, physical graphics, boot stability,
and rootfs tests run in the persistent development container.

One physical run then uses only the already staged kernel, root image, DTB,
and a small Stage1 replacement.  Acceptance requires the Baidu DOM, the direct
framebuffer PNG, zero Firefox restarts, MMC-only transport, verified hashes,
and automatic recovery to a fresh U-Boot epoch.  A single pass establishes the
new boundary; a second pass is required before calling it repeatable or pushing
the branch to remote `main`.
