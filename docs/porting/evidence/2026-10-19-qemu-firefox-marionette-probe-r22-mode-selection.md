# Firefox probe r22 mode-selection failure

## Result

r22 used the new rootfs and the detached host runner successfully kept the
QEMU process alive.  However, the host environment omitted
`ASTERINAS_FIREFOX_PROBE_ONLY=1`, so `DesktopM4DrmOperations` selected the full
scene boot arguments instead of the independent probe boot arguments.

The serial log contains `FIREFOX_DRM_PHASE name=new-session` and a
`WebDriver:NewSession` timeout, but no `FIREFOX_DRM_PROBE_*` marker.  This is a
mode-selection error, not a Firefox probe result.  It is recorded as
`inconclusive` and does not change the E3 guest diagnosis.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `7da4a148e1157c4d6c3370a7c533b8e999955971f818f2bc3349a7fe2fad6607` |
| result | `e55ab7e69fc74dfa794910743cc90039b75379d16136e707ce181de55a992e6e` |
| lifecycle | `45a0cf7f33f0dea2d221c52c7f20cf0aeaba36e8628d0ff91f1d2764fd6327e7` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r22-container/`

