# QEMU Firefox DRM scene r46: renderer marker audit

Date: 2026-10-17 (Asia/Shanghai)

This is a bounded host-side evidence step. It does not claim a QEMU runtime
receipt, a WebRender pass, or a physical-board result.

## Change

`firefox_drm_scene_gate.py` now includes the renderer observed by the scene's
DOM probe in the first `FIREFOX_DRM_SCENE_STATE` line. The value is normalized
to one bounded token (`renderer=...`) and falls back to
`document.documentElement.dataset.asterinasRenderer` when the WebGL query does
not populate the top-level renderer field. This makes the existing strict
renderer decision auditable from the stage marker without logging page text.

## Verification

Commands:

```text
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py
PYTHONPATH=tools/riscv python3 -m unittest -q \
  tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_firefox_webrender_gate
```

Result: `Ran 21 tests ... OK`; Python compilation passed.

A bounded fake-scene probe exercised the changed path and emitted:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=object done=1 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=999 timeout_type=function renderer=virgl webgl_context=1 webgl_debug=1 errors=0 last_error=none
FAKE_SCENE_MARKER renderer=virgl samples=32 webgl_context=1 webgl_debug=1
```

This is a host fixture check only, not a guest receipt.

The current strict Firefox boundary is unchanged: r44/r45 observed raw
`virgl` but Firefox sanitized it to `Generic Renderer` and failed WebRender
context creation. No image, frame, restart, or hardware claim is made here.
