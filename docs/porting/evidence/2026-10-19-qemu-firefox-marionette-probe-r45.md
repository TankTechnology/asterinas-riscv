# Firefox Marionette independent probe r45

r45 repeats the display-ready probe with `ASTERINAS_FIREFOX_PROBE_TIMEOUT=300`
instead of 150 seconds. The rootfs, kernel, QEMU provider, and protocol are
otherwise unchanged from r44.

## Result

The display contract remains green: bounded `xdpyinfo` wait passes and
glxtest reports virgl on `/dev/dri/renderD128`. Firefox creates Web Content,
RDD, WebExtensions, and Socket Process children, but the NewSession response
still never arrives within the longer bound. The host gate terminates QEMU
after its own bounded lifecycle; no scene or screenshot was attempted.

This disproves a simple cold-start budget explanation. The remaining work is
inside Firefox/guest process synchronization or graphics initialization.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `64a5aa230a48e6b7c6db9643f307f2cc0f50f3a1d79bd7cdb7c37de93489dd6c` |
| result | `dc92c96289c1325772cb812d37ca29a624d874f2e14c2a5ce36e1f0a863223f2` |
| lifecycle | `e280daa1d2c8dad7c1dc7b00cfb027e4851a47b8475e8a02a287fb78b22d24f1` |
| root image | `959581bb6ff5965f6ba7633d4d1ca5b9968bf66f0bd1419e3efdf55e5f25e68c` |
| root manifest | `a0c87476c7ad642684c2fa456a1013f7e82745af10e14d5222ede67d769e299d` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r45-container/`
