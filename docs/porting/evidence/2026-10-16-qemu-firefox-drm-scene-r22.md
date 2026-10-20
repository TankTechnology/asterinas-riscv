# QEMU Firefox DRM scene r22

r22 injected the current gate, scene HTML, and systemd service together, then
used a 30-second settle period followed by 2-second synchronous state polls.
The run was stopped after the diagnostic state marker; it did not run on real
hardware.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r22/serial.log).

## Source identity

- gate SHA-256:
  `1453e7ebb033e47a71b9b47aefef9f63b66f952853f42ff374a0aa5dff09ef1c`
- scene HTML SHA-256:
  `31ed5f5836586c4503fce39547866366d50e70f029ec16b28fddb3b59d080915`
- service SHA-256:
  `43a033bd66506b846ec50b0dd394b30db34834ce56f21c1fb62cf1b79abce901`
- serial SHA-256:
  `44da8cb6f6eee5cd52b3fc71de59697b0f37a09cfbb4da97e16c185ed1b0b75e`

## Boundary

The desktop and Marionette transport probe passed, and navigation reached the
current file URL. The state marker was:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0
```

No renderer, screenshot, frame, or recovery marker was emitted. This is a
fail-closed diagnostic, not a Task 6 pass. The next run must inspect script
element count/length and JavaScript availability while retaining the same
source hashes; the failure now occurs before the scene state object exists.

