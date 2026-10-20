# Firefox headless NewSession probe r20

## Result

r20 enabled Firefox's `--headless` flag while keeping the same official
Debian package, Asterinas kernel, and short Marionette probe.  It reached the
same boundary as the DRM desktop run:

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_DONE status=fail phase=new-session
```

Firefox's browser log still reports Marionette listening on port 2828, while
the MOZ renderer log is empty.  Since the headless process does not require
Xorg, EGL, or the virgl scanout for this command, the unchanged boundary
rules out the display/GL path as the direct cause of the NewSession stall.

## Interpretation

Task 6's remaining blocker is in Firefox's IPC/session initialization or an
Asterinas userspace primitive it uses after the socket greeting.  The next
diagnostic should focus on `epoll_pwait`, pipe/socket readiness, and child
process startup; no further DRM or GL changes are justified by this result.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `a0d1674b56c878dffcd73587e92615324ff3d8cea51542b5334e7e3699276cb6` |
| `result.json` | `bc352729a85efac0218e9d164fe343cc2ff5c46638fdbc38fce4beb1fb12edcf` |
| `lifecycle.json` | `57f50a5b843eca84f5efdea3148d2bb547901c8d9a7935644949313a3c386532` |
| kernel | `67643184cee924744fe6b890a42d7ac349d47c0aa5788cfac275da2a6d8ab378` |
| rootfs | `9d9188e7976efb4e57a70ab5e89aefdacece2cc5631649806e129ebecd8e86f3` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r20-headless-container/`
