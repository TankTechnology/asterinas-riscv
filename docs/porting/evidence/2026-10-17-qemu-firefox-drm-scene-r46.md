# QEMU Firefox DRM scene r46

Date: 2026-10-17 (Asia/Shanghai)

## Result

**FAIL CLOSED.** The current guest reached the scene and emitted the new
renderer-bearing stage marker, but Firefox reported `llvmpipe, or similar`.
The gate stopped before screenshot, frame timing, or restart acceptance.

The same run's independent probes reported Mesa `virgl` through GBM/EGL and
Firefox's bundled `glxtest`, so this run separates the Firefox renderer choice
from QEMU scanout and render-node access.

## Evidence

- Serial log: `serial.log`
- Structured result: `result.json`
- Serial SHA-256: `f583861a16568b8237d56d98de09a5dac6cfd738750753611b53231bc7197f47`
- QEMU: `asterinas/qemu-gl-env:20261014`, `virtio-gpu-gl-device`,
  `egl-headless`, `-nic none`
- Physical board: not used

The decisive lines are:

```text
FIREFOX_DRM_EGL_PROBE ... display=gbm renderer='virgl' ...
FIREFOX_DRM_GLXTEST ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128 ...
FIREFOX_DRM_SCENE_STATE ... renderer=llvmpipe,_or_similar webgl_context=1 webgl_debug=1
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_is_not_virgl:_llvmpipe,_or_similar
```

This is a QEMU runtime receipt. It does not prove physical GPU acceleration.
