# desktop-systemd-tmpfiles-dev r131

## Scope

A separate rootfs was materialized with a real systemd mask:
`/etc/systemd/system/systemd-tmpfiles-setup-dev.service -> /dev/null`.
The probe used generic QEMU, no display device, no network, and the same kernel,
U-Boot, DTB and Stage1. It did not start Xorg or Firefox and did not touch the
physical board.

## Marker table

| Boundary | Observed | Evidence |
|---|---:|---|
| `systemd-tmpfiles-setup-dev.service` | masked | no start marker in serial |
| `systemd-tmpfiles-setup-dev-early.service` | finished | `Finished systemd-tmpfiles-setup-dev-early.service` |
| `systemd-sysctl.service` | failed | `Failed to start systemd-sysctl.service` |
| `sysinit.target` | no | no `Reached target sysinit.target` marker in 120 seconds |
| Xorg / Firefox | no | no desktop or Firefox markers |

The mask was effective, so the absence of `sysinit.target` is not caused by the
normal `/dev` tmpfiles service. The remaining failure/wait boundary is in the
sysctl/systemd initialization path after the early tmpfiles unit.

## Artifacts

- masked rootfs: `/mnt/mydisk/asterinas-systemd-tmpfiles-mask-r131`
- runtime output: `/mnt/mydisk/qemu-desktop-systemd-tmpfiles-mask-r131`
- root image SHA-256: `2571a9d04930607826998865bbdcbef24a57b04763e4bf220affeb4b91595b32`
- serial SHA-256: `4f20eec1a5f9f1b8860fd317c313ad7a216d03add9915511ffc62f9ed0a3dd2c`
- lifecycle SHA-256: `8875f1ce9ca6efe40edf7ede5f04afa150d9b7462a22a42ef40d24004f3ad7fd`
- result SHA-256: `2b92e98ccbc73981e985490c90b4a9c4d72cf28d24f8f5c65fd622789646a63f`

## Conclusion

The ordinary `/dev` tmpfiles activation is not the hard blocker for sysinit.
The next gate should instrument the sysctl/systemd child completion path and
identify the first unit that remains pending after the sysctl failure. Do not
start desktop or Firefox yet.
