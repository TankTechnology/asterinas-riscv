# Firefox Marionette short probe r15

## Result

This was one bounded probe-only run after the virtio initialization markers
were added.  It reached Asterinas userspace, the DRM render node, Firefox
greeting, and the `NewSession` request boundary.  The response did not arrive
before the host probe deadline; no scene, screenshot, or recovery was run.

The decisive serial markers were:

```text
VIRTIO_GPU_INIT stage=pattern-done
VIRTIO_INIT stage=device-result type=Gpu ok=true
ASTERINAS_BOOT_PHASE phase=init-spawned
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PHASE name=probe-new-session
FIREFOX_DRM_PROBE_SAMPLE pid=135 state=R/S threads=50..59 rss=199240..260560 kB
```

The browser process remained alive and its thread count grew from 50 to 59,
then stabilized at 55 sleeping threads and about 260 MiB RSS.  The kernel
profile recorded active `futex(98)` and `ppoll(73)` calls, including long
futex waits near 29,809–29,994 jiffies.  The host gate terminated QEMU before
the guest `finally` block could publish its JSON probe receipt, so the serial
process samples are the authoritative bounded receipt for this run.

## Interpretation

The early virtio-gpu/DRM launch boundary is passed.  The remaining E3 boundary
is Firefox renderer/session initialization after greeting, with the same
futex/ppoll wait chain observed in r12.  This is a blocked NewSession result,
not a renderer or pixel result; do not run the 15-minute scene gate until the
next short diagnostic changes this boundary.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `74747fe31252e79c7e2d7cf46e96326a7279065787cf0d20686ddcbe9e94576d` |
| `result.json` | `352013e3fdfc39f17b63f5df03ecfd068c6c4a1e1d33af73bedf26383a4b1660` |
| `lifecycle.json` | `fdf810a239e7035be3ea3851785985e6db934008d90372e0f8514a21018ce197` |
| kernel | `36279042694a2538c9f31a99d803f5108d8da0fa06b19d91510868a1564fd549` |
| rootfs | `df7e8dcaee262cf1966eb4f0fbf6ad745d8ef20ea4f5a53e1faaaa77a69326ca` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r15-container/`
