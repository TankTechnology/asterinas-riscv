# Firefox Marionette short probe r2

The probe-only service reached the desktop but stopped at the 90-second
Marionette connect budget before the known cold-start greeting. It did not
send NewSession, navigate, capture, or run recovery.

Observed receipt markers:

```text
FIREFOX_DRM_PROBE_RECEIPT ... phase=connect status=fail
FIREFOX_DRM_PROBE_PROC pid=132 state=R threads=29 rss=126884 kB
FIREFOX_DRM_PROBE_THREAD ... (29 bounded thread records)
FIREFOX_DRM_PROBE_LOG name=browser bytes=0 ...
FIREFOX_DRM_PROBE_LOG name=moz bytes=0 ...
FIREFOX_DRM_PROBE_DONE status=fail phase=connect
```

`wchan` reads returned `FileNotFoundError` for every thread because this guest
procfs does not expose that file. The rest of the process/thread receipt was
written atomically and reached the serial log.

Artifacts: `/mnt/mydisk/qemu-firefox-marionette-probe-r2/`

| Artifact | SHA-256 |
|---|---|
| `desktop-m4-drm.serial.log` | `2f5c8170ffa5ada0236d01e129127b7d86d04413cdb1a6319159c106b7a4ec33` |
| `result.json` | `fa9c60a0374bd41187a8737ad8d8a056af2abbd1f68074a6e826831846353e29` |
| `lifecycle.json` | `3774529a283c4f56f2baf10c79846ba178b57a87242d52d2a8389478988f01b3` |

This is a probe timeout, not an E3 WebRender result. The next bounded probe
only needs a longer connect budget to reach the already observed greeting.
