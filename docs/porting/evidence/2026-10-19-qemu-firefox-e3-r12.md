# Firefox E3 runtime r12 (2026-10-19)

This run used the same official Firefox rootfs and matched QEMU/virgl runtime
as r11, with the host protocol fixed to wait for Firefox's terminal marker
before quitting QEMU.

## Exact application boundary

Independent graphics evidence passed:

```text
FIREFOX_DRM_EGL_PROBE rc=0 ... renderer='virgl'
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl DRM_RENDERDEVICE /dev/dri/renderD128
```

Firefox progressed farther than r11:

```text
FIREFOX_DRM_PHASE name=connect-state
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_PROCESS state=running
FIREFOX_DRM_WATCH ... rss_kib=258504 browser_log_bytes=950 mozlog_bytes=0
```

It never returned the Marionette greeting. The transport log records repeated
`ConnectionRefusedError` at `stage=tcp_connect`, and the bounded protocol
deadline expired. No `FIREFOX_DRM_SUPPORT`, scene renderer, screenshot, frame
samples, or recovery marker was produced. The E3 claim therefore remains
blocked even though the QEMU virgl path is healthy.

## Artifacts

`/mnt/mydisk/qemu-firefox-e3-r12-container/` retains the complete group:

```text
serial log 26e31f14490bb0b3bbe53da6a5c69b38d349ea8baeeea15cdeafebcc661444cc
result     a24e7f8f94afb640e13f6f0d361ed07ef85ccae4d3daf57e2fa85caee9321cd1
lifecycle  b39b8cf0fcbb86e4d287e7f4bdae0d6a85f828afd16191437b51c26f66b13ee9
```

This is the final bounded E3 runtime attempt for the current official package
and kernel pair; further progress requires Firefox/Marionette startup
debugging rather than another QEMU launch.

