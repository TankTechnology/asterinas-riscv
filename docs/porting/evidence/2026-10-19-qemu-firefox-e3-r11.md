# Firefox E3 runtime r11 (2026-10-19)

This bounded run used the official Debian Firefox rootfs overlay with the
matched QEMU 10.2.1 GL runtime, Mesa EGL, and AMD host render node
`/dev/dri/renderD129`. The complete gate ran inside the fixed GL container so
the guest file descriptors and temporary paths were shared correctly.

## Ordered boundary

Positive guest evidence reached the Firefox process:

```text
FIREFOX_DRM_ACCESS device=renderD128 ... read=1 write=1
FIREFOX_DRM_EGL_PROBE rc=0 ... renderer='virgl'
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl DRM_RENDERDEVICE /dev/dri/renderD128
FIREFOX_DRM_PHASE name=profile
FIREFOX_DRM_PHASE name=spawn-first
FIREFOX_DRM_PHASE name=connect
FIREFOX_DRM_PHASE name=connect-state
FIREFOX_DRM_PROCESS state=running
FIREFOX_DRM_WATCH pid=235 state=running rss_kib=34228 browser_log_bytes=0 mozlog_bytes=0
```

The run then stopped at the bounded Marionette `connect-state` boundary. It
produced no `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, scene screenshot, frame
samples, or recovery marker. The outer desktop classifier consequently
reported `missing DRM X11 pixel hash`; that is a gate-classification artifact,
while the Firefox-specific boundary is the missing Marionette response.

This is not an acceleration pass: the independent virgl probes pass, but the
official Firefox application did not reach WebRender evidence.

## Artifacts

The complete group is retained at
`/mnt/mydisk/qemu-firefox-e3-r11-container/`:

```text
serial log 6f6706fdfe9e9dfb3002509b1af515c72760e52e2dd5c8eeadb9e01c75534695
result     11e28d468ff53e9d886799b26e169cac40c7839a05f093b4340b6271a1ecc672
lifecycle  e0d4ca6e8c031803f98a774b45549345962896fa33bce70939b5779265647f97
```

Guest input hashes are recorded in `result.json`; the root image is the
official overlay input SHA-256
`aa26ef744b3c0e8568bbbaafbbfe300199ef14e354a698410b8b73bab4b80ad7`.

