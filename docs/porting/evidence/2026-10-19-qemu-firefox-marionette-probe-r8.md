# Firefox Marionette short probe r8

r8 is the first run with bounded direct-child process instrumentation. It reached the same Firefox boundary as r5/r6; no scene or screenshot was attempted.

## Runtime boundary

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_SAMPLE pid=127 state=S (sleeping) threads=58 rss=260200 kB schedstat=93811134900_18418191700_4384 syscall=present
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_RECEIPT path=/run/asterinas-firefox-marionette-probe.json sha256=989bc74acdc62ee323aac1af53a29a4fb8b624f4e34c8e6aad5fdce16fc9c718 phase=new-session status=fail
```

The final parent snapshot has 58 sleeping threads and stable scheduler counters. The new child records are:

```text
FIREFOX_DRM_PROBE_CHILD pid=398 name=Socket Process state=S (sleeping) threads=5 rss=36996 kB
FIREFOX_DRM_PROBE_CHILD pid=419 name=WebExtensions state=S (sleeping) threads=19 rss=85320 kB
FIREFOX_DRM_PROBE_CHILD pid=454 name=file:// Content state=S (sleeping) threads=14 rss=69900 kB
```

Firefox's browser log reports Marionette listening and `NewRenderer::Run is slow: 3498`. This localizes the boundary to renderer/session startup after the transport greeting; it does not prove a kernel scheduler defect. The child records show the renderer/content processes exist and are sleeping, so the next code investigation should inspect the Firefox renderer IPC/initialization path rather than retrying the same gate.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `65622a6d06e697a5a4c61fa89f84375ffec7303e6fc54a177b94b0d56c9c0263` |
| `result.json` | `768ffbad386c30895c76809895a560093c1a55f1d8ad7fe1c0f4f12797411c16` |
| `lifecycle.json` | `d5c44adf8bc06a7a2a0b9b9406649eea7607c85483a45e38e4bc27850a92c12d` |
| rootfs | `4b88a7a0518dda63d6927cd6dce245c7044cad1c7e8e1a6929be100388579dab` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r8-container/`

No scene, screenshot, frame, or recovery step was attempted.
