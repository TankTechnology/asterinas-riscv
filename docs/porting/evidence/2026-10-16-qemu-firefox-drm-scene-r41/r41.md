# QEMU Firefox DRM scene r41 (diagnostic only)

r41 was rebuilt after temporary-image cleanup from the fallback rootfs, so its
Firefox scene HTML was stale (`script_bytes=956`). The run was stopped before
scene acceptance and is not a Task 6 scene receipt. Its pre-Firefox diagnostics
are still valid and were archived:

```text
FIREFOX_DRM_ACCESS groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_EGL_PROBE rc=0 bytes=125 text=PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST rc=0 bytes=211 text=DRI_DRIVER virtio_gpu VENDOR Mesa RENDERER virgl VERSION 3.1 Mesa 25.0.7-2+deb13u1 TFP TRUE DRM_RENDERDEVICE /dev/dri/renderD128 MESA_VENDOR_ID 0x1af4 MESA_DEVICE_ID 0x1050 DDX_DRIVER modesetting; TEST_TYPE EGL
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=none dom_done=0 dom_samples=0 scripts=1 script_bytes=956 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
```

The glxtest result confirms Firefox's own EGL probe sees virgl and the render
node. The stale scene prevents any WebRender or screenshot claim; the next
run must rebuild from the current r40 image or inject both current gate and
scene sources before evaluating Firefox WebGL.
