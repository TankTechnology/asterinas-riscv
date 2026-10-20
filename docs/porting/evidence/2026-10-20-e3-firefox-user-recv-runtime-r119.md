# E3 Firefox user-recv runtime r119

- Gate: `e3-firefox-user-recv-runtime`
- Input kernel SHA-256: `54dd701c273b296c783269973bb1328abcecc6c0a0aea2a3c14bd13e8a6badad`
- Output: `/mnt/mydisk/qemu-firefox-e3-r119-user-recv/desktop-m4-drm.serial.log`
- Lifecycle: `/mnt/mydisk/qemu-firefox-e3-r119-user-recv/lifecycle.json` (`status=failed`, `reason=protocol`)
- Result: `/mnt/mydisk/qemu-firefox-e3-r119-user-recv/result.json` (`passed=false`)

## Boundary table

| Assertion | Result | Evidence |
|---|---|---|
| GL renderer is virgl and render node is present | pass | `firefox-drm-scene-gate[93]: FIREFOX_DRM_GLXTEST rc=0 bytes=211 text=DRI_DRIVER virtio_gpu VENDOR Mesa RENDERER virgl VERSION 3.1 Mesa 25.0.7-2+deb13u1 TFP TRUE DRM_RENDERDEVICE /dev/dri/renderD128 MESA_VENDOR_ID 0x1af4 MESA_DEVICE_ID 0x1050 DDX_DRIVER modesetting; TEST_TYPE EGL` |
| NewSession request was framed and sent | pass | `begin` and `send_complete` markers are present (`0/0`) |
| Kernel user-recv delivered the 66-byte request to Marionette port 2828 | pass | `[32m[   205.814][0m [31mERROR [0m: [39mbigtcp: ASTERINAS_TCP_TRACE seq=25 stage=user-recv connection=2795038624 local_port=2828 remote_port=33400 value0=66 value1=0 value2=0[0m` |
| NewSession response frame arrived within 360 seconds | **fail** | no response marker after `send_complete`; lifecycle ended `reason=protocol` |

The `user-recv` record has `local_port=2828`, `value0=66`, and is the server-side
receive of the NewSession request. Thus the guest TCP receive path delivered the
request; the remaining failure is in Firefox's post-receive request handling or
response generation. No claim of a futex fix is made by this receipt.

Serial SHA-256: `53162ad67203ee75e8389ec768bf98a2c1a1d487bd0d5ea1f475043bb0265a9a`
Lifecycle SHA-256: `913f1b4c77e111c1d29698aeb2075ef6087f6176493f8b111637bb62d8e373db`
Result SHA-256: `6a161c08ac723a70e7a4523ffbd31ef4bc8b6233f4ad8decbb6a16c0727b1e7b`
