# Firefox Marionette independent probe r44

r44 adds the bounded Xorg readiness wait before glxtest and Firefox startup.

## Result

The ordering defect is fixed:

```text
DEBIAN_DESKTOP_M4_XORG_START provider=drm status=ready
FIREFOX_DRM_DISPLAY_WAIT status=pass attempts=23 elapsed_ms=44843.8
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
```

Firefox then creates Web Content, RDD, WebExtensions, and Socket Process
children. Its browser log reports:

```text
RenderThread::InitDeviceTask is slow: 3689
```

The NewSession response still does not arrive before the 150-second bound.
This closes the display-order defect but leaves the Firefox render-thread and
content-process synchronization boundary unresolved.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `2fb169214a515e6cfa6b3ef89fc1e7677fa22b60f3b9fa1a04ec4166ffd6de2a32` |
| result | `2c51a9f1c3ef7fa9b2c1ef88df15b4e30a66b321fa32c944ebc180e2f4b1ded2` |
| lifecycle | `7c487d737b5ba47aba29604a3d0f61f52088980a7e1a6e33208408e398515057` |
| root image | `2f90012e721ab3b47cfc148a7298ebf9f793cbe1f03eb406a05af744231570db` |
| root manifest | `f8f9db970ed8ff2983dbb6482ef32673de23e08d43f9bd51783cb68e6949e761` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r44-container/`
