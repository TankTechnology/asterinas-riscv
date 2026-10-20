# desktop-systemd-sysinit r130

## Scope

One bounded generic-QEMU run (120 seconds) used the same kernel, U-Boot, DTB,
Stage1 and qcow2 root image as r128/r129. No Xorg, Firefox, network, physical
board, or kernel modification was used.

## Marker table

| Boundary | Observed | Evidence |
|---|---:|---|
| Stage1 rootfs handoff and systemd exec | yes | carried forward from r128; same input set |
| `sysinit.target` | no | no `Reached target sysinit.target` marker |
| `systemd-sysctl.service` | failed | `Failed to start systemd-sysctl.service` |
| `systemd-tmpfiles-setup-dev.service` | started but did not finish | `Starting systemd-tmpfiles-setup-dev.service` with no corresponding `Finished` |
| udev trigger | finished | `Finished systemd-udev-trigger.service` |
| sys-kernel-config.mount | mounted | `Mounted sys-kernel-config.mount` |
| Xorg / Firefox | no | no desktop or Firefox markers in the transcript |

The last ordered systemd operation before the 120-second boundary was
`systemd-tmpfiles-setup-dev.service`; the run does not prove that sysctl is the
hard blocker.

## Artifacts

- output: `/mnt/mydisk/qemu-desktop-systemd-sysinit-r130`
- serial SHA-256: `1134ba0c5780527455a316bd5e71cba1ec0cd06bf8618f9c0153710ec33cf137`
- lifecycle SHA-256: `59432d9109ab666d4ba495591c930769f5cc711d8566711c87a6c8ca3e20fcc6`
- result SHA-256: `999d15aafdab3c3412a6ffbc5d65fefe40c47bbfe4555a66f0295144426ef889`

## Conclusion

The next independent boundary is `desktop-systemd-tmpfiles-dev`: inspect the
unit's command and `/dev` tree interaction, then determine whether the service
is slow or blocked on an unsupported filesystem/device operation. Do not start
Xorg or Firefox until sysinit/basic activation is independently observed.
