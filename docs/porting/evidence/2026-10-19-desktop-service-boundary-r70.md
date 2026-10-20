# M4 desktop service boundary — r70

## Scope

One bounded QEMU run checked only whether the single M4 service reaches its
session entrypoint after removing the PAM/TTY handoff.  Firefox, DRM, and real
hardware were not tested.

The base image `/mnt/mydisk/qemu-desktop-phase-r71` was not modified.  A
derived ext2 image was made with the audited overlay
`/mnt/mydisk/m4-r69-overlay/`:

| Artifact | SHA-256 |
| --- | --- |
| overlay.json | `d026afc0263dcfdad581ea7b6b019d78517fb8323fbc0a515cb5301e13c41d39` |
| desktop-m4.service | `eb43efef26b99a0026fc0a39776ca80d33cceb1460d019784ce3ddfccc636e6c` |
| desktop-m4-ordered.service (no-op alias) | `2560642ddf93a410b9dfcf4b8b6a2897ccd3002392133c7363351e55759ddd30` |
| derived root image | `f9b345512f5d2f3f3dd46d8aa5b7fc1dfeda33d3ce63e0040d938b43b768f7c4` |
| derived manifest | `dd2d855c066a9c9a403f014f20d34ad3c6da02559dc855b7a400706f98765388` |
| serial log | `83dbd305c88bd1b050694ff593c456c3dbf0438e89f17a0b678c637221c69a8e` |

## Runtime result

The run used the existing kernel/DTB/Stage1/U-Boot artifacts and a 60-second
boot deadline.  `lifecycle.json` records `status=failed`, `reason=protocol`;
this is a deliberate fail-closed result because the legacy classifier still
requires the old complete desktop marker set.

The new boundary itself passed.  The serial log contains, in order:

```text
Started asterinas-desktop-m4.service - Asterinas Debian desktop session.
desktop-m4-session[79]: ASTERINAS_DESKTOP_M4_SESSION phase=start
```

The log also shows the device-access preflight completed before service start.
There is no PAM/TTY startup failure in this run.  The next required change is
to update the M4 classifier/evidence contract to accept the new session marker
and then run one separate bounded desktop readiness check.

## Input identity

| Input | SHA-256 |
| --- | --- |
| kernel | `245d54df230f0d8878411defd982d5e4189d469f6172379c6c246f56c9fba1e` |
| DTB | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| Stage1 | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| U-Boot | `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210` |

