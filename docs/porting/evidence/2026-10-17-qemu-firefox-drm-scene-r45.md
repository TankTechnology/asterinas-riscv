# QEMU Firefox DRM scene r45

## Result

This diagnostic run retained the r44 hardware-feature override and added only
`MOZ_GFX_SPOOF_GL_RENDERER=VideoCore IV HW`, a Mozilla graphics-information
spoof intended to test whether the blocklist identity alone was responsible.
The independent GBM/EGL and `glxtest` checks still report `virgl`.

Firefox still logs:

```text
Couldn't sanitize GL_RENDERER "virgl"
Failed GL context creation for WebRender
Failed to connect WebRenderBridgeChild
```

The page reaches `webgl_context=1`, but the exposed renderer is
`Generic Renderer`, so the strict gate rejects the run before screenshot,
frame, or restart stages. The spoof does not affect the sanitizer or the raw
WebRender GL context. It is diagnostic-only and is not a product workaround.

## Reproducibility

- Serial log: `serial.log` (`beb327874967328ac59d49c9baf16c85beb5b0ecb8cca5011e3d9ae6a2688567`)
- Gate source: `6d72ea70fff309cc37c8614603b02d90553114b048df686b4f1e121b3d0f5ae0`
- Scene source: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`
- Service source: `81b6db6adf5afa481687a1f7bed449fe5239e6644fadfd9ba68801c563f2d584`
- Diagnostic profile: `gfx.blacklist.webgl-use-hardware=1`
- Diagnostic environment: `MOZ_GFX_SPOOF_GL_RENDERER=VideoCore IV HW`
- Structured result: `result.json`
- QEMU only; no physical board access or hardware MMIO was performed.
