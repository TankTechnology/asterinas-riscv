# Firefox E3 runtime r17 (2026-10-19)

## Input and boundary

This was the single bounded runtime attempt using the corrected official
Firefox overlay. It used the existing GL-capable QEMU 10.2.1 and did not touch
the physical board.

- Rootfs SHA-256: `59f3b672134c19c221dbcb6f996e90764f8165ed5ff62b58f6cea7e44a22fbce`
- Rootfs manifest SHA-256: `189142424e7bcb5c2e9251b119c9536900d84054c1d5a88c34807ffb2a9b9db3`
- QEMU SHA-256: `d4a95ece1ea69687257cb584182bce231d8f379fda0231ec62d4da07611e1952`
- Kernel SHA-256: `1660a4606f88d3131ad030a489b978a0f3a24fed4d17beb6db31c2829029a02d`
- DTB SHA-256: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`
- Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
- Scene HTML SHA-256: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`

## Observed evidence

The guest reached the DRM desktop contract:

```text
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

Firefox itself started and selected the GPU path:

```text
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
FIREFOX_DRM_PHASE name=connect-state
FIREFOX_DRM_PHASE name=new-session
A_WEB_MARIONETTE_TRANSPORT ... stage=tcp_connect ... ConnectionRefusedError
```

No Marionette greeting, `about:support`, fixed-scene screenshot, frame sample,
or recovery marker was produced before the 900-second bound. The gate stopped
with `reason=protocol` and `passed=false`.

## Artifacts

Output group: `/mnt/mydisk/qemu-firefox-e3-r17-container/`

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `c627b0f9fb849b98200a6b19dd7eaaa8cdb80d7bc4c6645848375fe0bd6d98be` |
| `result.json` | `7ad1ffd32c3a760d4e40c4a83d44ba4444b627161d4b10b9c11ed9cb41765de6` |
| `lifecycle.json` | `af16c79f1f18929c40be9d108788d235f68e97e6c6c54ea23ce2cc54161f24ee` |

## Result

E3 remains blocked at the same Marionette TCP-connect boundary. Virgl and
`renderD128` use are independently proven, but Firefox application-level
WebRender evidence is still missing. Per the two-failure rule, no further E3
runtime retry is allowed without changing the external condition or debugging
the Firefox/Marionette startup path.
