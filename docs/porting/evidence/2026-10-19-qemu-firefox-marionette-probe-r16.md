# Firefox Marionette short probe r16

## Result

r16 used the current instrumented kernel and the 150-second guest probe
deadline.  It reached every boundary needed to classify the Firefox block:

```text
VIRTIO_GPU_INIT stage=pattern-done
ASTERINAS_BOOT_PHASE phase=init-spawned
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=fail type=TimeoutError
FIREFOX_DRM_PROBE_RECEIPT ... phase=new-session status=fail
FIREFOX_DRM_PROBE_DONE status=fail phase=new-session
```

The guest receipt reports a live Firefox parent with 53 threads and a direct
`Socket Process` child with five threads.  The thread snapshot includes
`futex(98)` and `ppoll(73)` waits.  The browser log says Marionette is
listening on port 2828; the only browser warning is a missing optional
`dbus-launch` helper.  The MOZ renderer log is empty because the session never
started.  Therefore this is a Firefox NewSession/IPC initialization block,
not a TCP greeting failure, DRM render-node failure, or virtio-gpu launch
failure.

## Interpretation

E3 remains blocked at the same NewSession boundary, now with a complete
atomic guest receipt and periodic process/log snapshots.  No scene, screenshot
or recovery phase was attempted.  The next investigation should compare
Firefox's socket/content child IPC waits against the Asterinas futex/ppoll
contract; another full scene run would not add evidence.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `d416e3673d8ff6658b50de7297fbf5256078d5c7a8bee6c4216ca93c0499ef3c` |
| `result.json` | `75f3fb1907829829583b3dfd18232224197dafaed7a81b5a03969ad4c4bc5b2c` |
| `lifecycle.json` | `0a06082b3fcb273b4be0d73e82c43a052dcc557a32736fbe9aa8a37ff9250c74` |
| guest probe receipt | `3f6f65869e856bee470dddb537ff54289f954dadda7c50176878e1fd47140fdb` |
| kernel | `36279042694a2538c9f31a99d803f5108d8da0fa06b19d91510868a1564fd549` |
| rootfs | `1b397b2f194e8c73d7d070408b11f701aa8a0f96abfbe0b698abf64eca03bb06` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r16-container/`
