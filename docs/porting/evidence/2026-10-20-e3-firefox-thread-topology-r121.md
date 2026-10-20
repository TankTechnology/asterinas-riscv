# E3 Firefox post-NewSession thread topology r121

- Gate: `e3-firefox-thread-topology`
- Source: `/mnt/mydisk/qemu-firefox-e3-r119-user-recv/desktop-m4-drm.serial.log`
- This is read-only analysis of r119; no QEMU or Firefox was started.

| Boundary | Observation | Classification |
|---|---|---|
| NewSession request | r119 already records server-side `user-recv local_port=2828 value0=66` | delivered to Marionette |
| Firefox worker wait | `ASTERINAS_SYSCALL_PROFILE slow=47 pid=467 tid=480 syscall=98 name=futex elapsed_jiffies=43934` | longest observed post-request futex wait |
| Firefox worker poll | `ASTERINAS_SYSCALL_PROFILE slow=48 pid=467 tid=467 syscall=73 name=ppoll elapsed_jiffies=37604` | longest observed post-request ppoll wait |
| Parent process | `firefox-drm-scene-gate[93]: FIREFOX_DRM_WATCH pid=237 state=running rss_kib=259576 browser_log_bytes=807 mozlog_bytes=0` | remains running; RSS/watch marker persists |
| NewSession response | no response frame before the 360-second boundary | failed |

The wait topology is in Firefox's post-receive processing (a worker futex plus
polling thread), while the parent remains alive.  Together with the r105
synthetic futex/ppoll reproducer and the r119 socket-delivery trace, this is
stronger evidence for an application-level Firefox wait dependency than for a
lost TCP receive.  It does not prove that every futex path is correct and does
not justify a kernel futex patch.

Serial SHA-256: `53162ad67203ee75e8389ec768bf98a2c1a1d487bd0d5ea1f475043bb0265a9a`
