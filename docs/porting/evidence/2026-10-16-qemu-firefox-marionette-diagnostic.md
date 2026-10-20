# QEMU Firefox Marionette diagnostic — 2026-10-16

This is a bounded diagnostic run on the existing DRM-only QEMU desktop. It did
not access the development board. The disposable rootfs was a copy of the
browser DRM image with only the gate entrypoint replaced by a diagnostic
program; no repository source was injected into the accepted Task 6 path.

## Probe contract

The probe used the GL-capable QEMU backend with `egl-headless` and
`virtio-gpu-gl-device`, no NIC, and the existing DRM desktop boot arguments. It
started Firefox at `about:blank`, then planned the following ordered commands:

```text
NewSession -> SetWindowRect -> ExecuteScript(return 1)
-> Navigate(data:...) -> ExecuteScript(document.readyState)
-> Navigate(file:///usr/share/asterinas/firefox-drm-scene.html)
-> ExecuteScript(scene marker type)
```

Each command had a bounded Marionette timeout and the run had a bounded outer
timeout.

## Result

The guest reached Xorg and the Marionette socket. It emitted:

```text
FIREFOX_DRM_DIAG spawn pid=117
FIREFOX_DRM_DIAG connected
FIREFOX_DRM_DIAG command-start command=WebDriver:NewSession
```

No `command-done`, `ExecuteScript`, page, renderer, screenshot, frame, or
recovery marker was emitted before the run ended. The result is therefore a
fail-closed session-establishment diagnostic; it does not prove a WebRender or
virgl result. In this A/B, the `about:blank` start URL itself is a new
variable that stalls before `NewSession`; it must not replace the r16 local
scene start URL. The next implementation keeps the r16 start URL and only
adds explicit window-handle selection before navigation/script commands.

```text
passed=false
reason=NewSession did not return before the bounded diagnostic run ended
serial_sha256=e3799957840b0a623a1e39e1931b9c5b9150faf87feb954e01c6d3a47fe01f9
```

Raw serial and structured result are stored beside this document. This
receipt is separate from the r16 scene receipt and does not change Task 6's
status.
