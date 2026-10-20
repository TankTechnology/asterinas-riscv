# QEMU Firefox DRM scene r47

Date: 2026-10-17 (Asia/Shanghai)

## Result

**FAIL CLOSED.** This was one diagnostic A/B based on the Firefox ESR source
path: the temporary profile kept `gfx.blacklist.webgl-use-hardware=1` so the
raw virgl path was selected, and added
`gfx.egl.prefer-gles.enabled=true`. The gate also enabled bounded
`RenderThread/WebRender/Widget/Layers` logging.

The preference did not produce a valid WebRender receipt. The scene marker
reported `renderer=Generic_Renderer`; the browser diagnostic contained both
`Couldn't sanitize GL_RENDERER "virgl"` and `Failed GL context creation for
WebRender: 0`. The independent GBM/EGL and glxtest probes remained `virgl`.
The run stopped before screenshot, frame, and restart acceptance.

This rules out “GLES preference alone fixes the boundary”; it does not prove a
3.1/3.2 incompatibility either. The next action belongs in the Firefox source
/build path, with a patched package provenance, rather than another runtime
preference combination.

## Evidence

- Serial log: `serial.log`
- Structured result: `result.json`
- Serial SHA-256: `1904f1bbfade24b0992712c67a00700c85ad1fb91bc6ff166e785150194317ee`
- QEMU: `asterinas/qemu-gl-env:20261014`, `virtio-gpu-gl-device`,
  `egl-headless`, `-nic none`
- Physical board: not used

The r47 result is a QEMU runtime receipt, not native PowerVR evidence.
