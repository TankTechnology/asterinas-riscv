# Firefox short NewSession probe r5

## Runtime result

This was one bounded headless DRM probe with a 150-second Firefox probe
deadline; it did not run the fixed scene, scroll, animation, or video gate.
The guest reached the existing DRM/virgl desktop readiness markers. Firefox
then produced a Marionette greeting, but `WebDriver:NewSession` timed out
after 141.8 seconds.

Relevant guest records:

```text
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_PROC pid=130 state=R threads=53 rss=213952 kB
FIREFOX_DRM_PROBE_CHILD pid=399 name=Socket Process state=S threads=5 syscall=ppoll(73)
FIREFOX_DRM_PROBE_CHILD pid=418 name=WebExtensions state=R threads=14
FIREFOX_DRM_PROBE_CHILD pid=425 name=RDD Process state=S threads=2 syscall=ppoll(73)
FIREFOX_DRM_PROBE_LOG name=browser bytes=443 ... Marionette INFO Listening on port 2828
FIREFOX_DRM_PROBE_DONE status=fail phase=new-session
```

The aggregate syscall channel recorded Firefox futex/ppoll/epoll activity,
including parent futex and child ppoll waits. The new detail markers
(`ASTERINAS_MMAP_DETAIL`, `ASTERINAS_EPOLL_PROFILE`, and
`ASTERINAS_FUTEX_*_DETAIL`) were absent in this boot despite their boot
parameters. Therefore the logger serialization change is compile-verified but
not yet runtime-verified, and the detail-parameter path itself is now the next
small diagnostic boundary.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `13d8137391515cbb3b0fb8c00910a3f48d398f104b55d538af4879fd9937fa03` |
| `result.json` | `ad3d06bd1401674b5b651a8ec98008e771a811e8d8e6b73c2d535282af854041f` |
| `lifecycle.json` | `6c07edea2535ceee1004f08f3b1d0401574576ad1c560e173c5c4c4614d02f5` |

Output group: `/mnt/mydisk/qemu-firefox-log-channel-r5/`.
