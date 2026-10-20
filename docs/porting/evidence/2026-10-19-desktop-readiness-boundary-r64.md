# Desktop readiness boundary r64

This is a bounded QEMU diagnostic with the official root image, no Firefox workload, and `ASTERINAS_DESKTOP_BROWSER_ENABLED=0`. The environment variables were inserted before `init=/init`; Stage1 therefore completed normally.

Observed order:

1. `DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec`
2. systemd reached `basic.target` and `multi-user.target`
3. systemd emitted `Starting asterinas-desktop-m4-evidence.service`
4. systemd emitted `Starting asterinas-desktop-m4.service`
5. No `DEBIAN_DESKTOP_M4_*_READY`, `DEBIAN_DESKTOP_M4_FAIL`, or Xorg/session marker appeared before the 60-second host bound.

The browser-disabled run therefore narrows the stall to the desktop unit's `ExecStartPre`/session/Xorg boundary, before the evidence script can publish a ready or failure marker. It rules out Netsurf/Firefox as the cause of this desktop readiness stall. The exact sub-phase is not yet observable because the session and Xorg logs are on `/run` tmpfs and the current script only emits its diagnostic after a longer 240-second guest deadline.

Artifacts:

- serial: `/mnt/mydisk/qemu-desktop-core-diagnostic-r64/desktop-m4.serial.log` (SHA-256 `d80e3c640c0ec6dd05e51f55844d19ba26a0000e42704f322d0ecc3a10ffbf16`)
- result: `/mnt/mydisk/qemu-desktop-core-diagnostic-r64/result.json` (SHA-256 `dda603426e2471c96a576886c7d95527cb2e75705e0dd2157b9bb293babcebcd`)
- lifecycle: `/mnt/mydisk/qemu-desktop-core-diagnostic-r64/lifecycle.json` (SHA-256 `ce12e6d17e59ecf21a551ea05a4ab8dfc65f958c4f095b8ef5aafdc2eca5d55d`)
