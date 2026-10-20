# Firefox E3 runtime r18 (2026-10-19)

## Purpose

This was the only runtime attempt after changing the initial Marionette
`NewSession` payload to the minimal `{strictFileInteractability: true}` form.
The purpose was to distinguish capability negotiation from Firefox startup.

## Result

The change did not move the failure boundary:

```text
A_WEB_MARIONETTE_TRANSPORT ... frame_header ... body_expected=50
FIREFOX_DRM_PHASE name=new-session
A_WEB_MARIONETTE_TRANSPORT ... request_id=1 ... command=WebDriver:NewSession ... send_complete=true
FIREFOX_DRM_WATCH ... rss_kib=258932 browser_log_bytes=807
```

The guest reached the desktop and Firefox GL path before the bounded protocol
deadline:

```text
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
```

No NewSession response, fixed-scene state, screenshot, frame samples, or
recovery marker was produced. The gate ended with `passed=false`,
`reason=protocol` after the 900-second bound.

## Inputs and artifacts

- Rootfs SHA-256: `db27606f172ad3b1f894c45f11e03193dcb16be378b67a8a240447c1b18144fe`
- Rootfs manifest SHA-256: `832861903677f20cd432d46f8fb095fa54fd4c826b6c2473b5e44f5d4d5f79a3`
- Kernel SHA-256: `1660a4606f88d3131ad030a489b978a0f3a24fed4d17beb6db31c2829029a02d`
- DTB SHA-256: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`
- Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
- Scene HTML SHA-256: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`

Output group: `/mnt/mydisk/qemu-firefox-e3-r18-container/`

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `dfb77a0613a240bb8d6ee692d802f7c0b4501c54a2693a80d83c846f366cfe8e` |
| `result.json` | `b20cc51c85181d6b8698d55116e10b6163207cbcc53eb1ad7e0d9f3c3e0b955e` |
| `lifecycle.json` | `82d9be28db408c3a58bc5c56aa8db23c1242bb63b7f376acad546c534ad3b5ee` |

## Interpretation

The minimal capability payload does not change the boundary. This is now a
Firefox/guest-side NewSession initialization stall, not a capability-shape or
render-node discovery problem. Per the two-failure rule, further long QEMU
runs are frozen until an independent short diagnostic changes the condition.
