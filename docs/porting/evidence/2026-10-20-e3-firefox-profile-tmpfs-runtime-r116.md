# E3 Firefox `/run` profile runtime boundary r116

- gate: `e3-firefox-profile-tmpfs-runtime`
- claim: E3 diagnostic runtime boundary
- status: `fail`
- input: official Debian trixie riscv64 Firefox ESR `140.16.0esr-1~deb13u1`, profile `desktop-m4-firefox`
- rootfs: `/mnt/mydisk/asterinas-firefox-rootfs-20261019-preseed/debian-root.ext2`
- output: `/mnt/mydisk/qemu-firefox-e3-r116`
- profile override: `/run/asterinas-firefox-probe`
- capability arm: `minimal` (`strictFileInteractability: true`)
- bound: `360s` host boot/probe budget; no retry

This is one bounded runtime probe after the profile contract was changed to use
`/run`.  It does not claim E3 completion and it does not run the full scene.
The QEMU process was cleaned up at the deadline; no process remained afterward.

## Observable boundary

The guest reached the Firefox process and the GL path before the protocol
stall:

- `FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128`
- `FIREFOX_DRM_PROCESS state=running`
- `A_WEB_MARIONETTE_TRANSPORT ... command="greeting" ... stage="response_body" ... header_bytes=3 ... body_expected=50`
- `A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:NewSession" ... stage="send" ... send_complete=true`
- no subsequent `WebDriver:NewSession` response marker before the 360s bound
- watchdog snapshots show the Firefox parent still `state=running`; the trace contains
  `syscall=73 name=ppoll` and `syscall=98 name=futex` waits
- final lifecycle: `phase=finished`, `status=failed`, `reason=protocol`

The profile path was therefore accepted and the renderer remained `virgl`, but
moving the profile to `/run` did not close the NewSession response boundary.
This does not prove the profile was the cause of the earlier stall.  The
remaining evidence points to the same Firefox/userspace wait boundary already
recorded by r99/r106, rather than to a capability or profile-path fix.

## Artifacts

- lifecycle: `/mnt/mydisk/qemu-firefox-e3-r116/lifecycle.json`
- result: `/mnt/mydisk/qemu-firefox-e3-r116/result.json`
- serial: `/mnt/mydisk/qemu-firefox-e3-r116/desktop-m4-drm.serial.log`

## Verification

- one QEMU run, bounded at 360s; no retry
- QEMU process check after completion: no `qemu-system-riscv64` process
- no board, serial device, network, or hardware operation
- this diagnostic receipt does not satisfy required E3 assertion `official_firefox_scene`

Next boundary: freeze the profile hypothesis and use the existing ppoll/futex
correlation evidence; do not start another long scene gate from this result.
