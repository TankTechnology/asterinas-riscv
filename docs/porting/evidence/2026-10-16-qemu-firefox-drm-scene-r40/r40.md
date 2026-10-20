# QEMU Firefox DRM scene r40

r40 used the current gate and the permanent scene-unit `render` group. Before
Firefox, the gate recorded both render-node access and a user-level EGL/GBM
pixel probe:

```text
FIREFOX_DRM_ACCESS groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_EGL_PROBE rc=0 bytes=125 text=PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
```

Firefox still failed its WebGL context creation, and the bounded browser
summary exposed the actual reason:

```text
* WebglAllowWindowsNativeGl:false restricts context creation on this system. ()
* Exhausted GL driver options. (FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS)
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

This separates the layers: the same unprivileged service can open the render
node and Mesa/virgl can render pixels, while Firefox's own GL driver probing
still rejects the context. The run is fail-closed and has no WebRender,
Firefox virgl, screenshot, frame, or restart receipt. The next gate must focus
on Firefox's GLX/EGL driver-probe result (including its GPU-process/glxtest
path), not on DRM node permissions or further profile toggles.
