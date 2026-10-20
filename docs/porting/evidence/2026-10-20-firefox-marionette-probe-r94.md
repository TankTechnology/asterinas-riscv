# Firefox Marionette short probe r94

- Gate: `firefox-marionette-probe-socket-boundary` (E3 supplemental; does not satisfy `official_firefox_scene`)
- Runtime: one bounded `PROBE_ONLY=1` QEMU run; no Firefox scene navigation or screenshot
- Rootfs: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-probe-r94`
- Output: `/mnt/mydisk/qemu-firefox-probe-r94`

## Observed boundary

The display and renderer path still passed:

```text
glamor X acceleration enabled on virgl
DEBIAN_DESKTOP_M4_XORG_START provider=drm status=ready source=drm-log pid=142
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128 ...
```

Firefox started multiple child processes and its browser log contained:

```text
Marionette INFO Listening on port 2828 [Parent 304, Main Thread]
```

The new bounded procfs diagnostic reported no entries for both
`/proc/net/tcp` and `/proc/net/tcp6` at every sample. A source audit confirms
that this Asterinas tree has no `/proc/net/tcp*` procfs implementation, so the
original compact `states=none` marker cannot distinguish a missing file from
an empty table. The probe is being tightened to emit explicit
`tcp=error:FileNotFoundError`/`tcp6=error:FileNotFoundError` markers on future
runs. The first greeting
event eventually produced a frame header, but `WebDriver:NewSession` sent its
request and then timed out waiting for a response header:

```text
FIREFOX_DRM_PROBE_SOCKET port=2828 states=none
A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:NewSession" stage="send_complete"
A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:NewSession" stage="response_header" ... error_type="TimeoutError"
FIREFOX_DRM_PROBE_RECEIPT ... phase=new-session status=fail sample_count=4
FIREFOX_DRM_PROBE_DONE status=fail phase=new-session reason=TimeoutError:_Marionette_read_deadline_expired
```

## Interpretation

This run rules out “Firefox never started” and “virgl was not selected”. It
also shows that `/proc/net/tcp*` is unavailable as a listener oracle on this
guest: the Firefox log says it is listening while the procfs files are not
implemented. The actionable runtime boundary remains the NewSession response path
(likely the kernel IPC/futex/TCP receive path), not the DRM renderer. The
host lifecycle later failed its final root-hash handoff (`reason=hash-final-root`)
after the guest probe had already emitted its atomic receipt; this is recorded
separately and is not treated as a Firefox renderer result.

## Artifacts

- Serial log: `/mnt/mydisk/qemu-firefox-probe-r94/desktop-m4-drm.serial.log`, SHA-256 `6168150bd5a14510cd3371522c084ddffabf2b3fc6364054961e2b3a42e8a744`
- Lifecycle: `/mnt/mydisk/qemu-firefox-probe-r94/lifecycle.json`, SHA-256 `5e05ccbc7aea0d5bb413982949f4f656b1c00f9df10dd96aebe1d623cc5d2839`
- Result: `/mnt/mydisk/qemu-firefox-probe-r94/result.json`, SHA-256 `10180beefd2be4bda8cecaaec4b04bf9dccc6af22a8294b91a3d5817e9c80cef`
- Probe root image: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-probe-r94/debian-root.ext2`, SHA-256 `f4542e50732d6d1d8d37a13e230b2ffb9fd7f7ad221201be28db6a96eda89fe7`

No E3 pass is claimed; no full scene gate was run.
