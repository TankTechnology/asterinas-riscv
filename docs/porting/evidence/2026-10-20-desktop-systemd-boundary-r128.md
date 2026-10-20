# desktop-systemd-boundary r128

## Scope

This bounded generic-QEMU probe used the same kernel, U-Boot, DTB, Stage1 and
Firefox-profile root image as r126, with no display device, no network, and
`systemd.mask` for the desktop and Firefox services. It ran for at most 120
seconds and did not touch physical hardware.

The r127 diagnostic exposed an argument-ordering error: `systemd.unit` and
`systemd.mask` had been placed after the `--` separator and were rejected by
Stage1 as root-init arguments (`DEBIAN_ROOTFS_FAIL reason=root-init-argument`).
This run places those systemd parameters before `init=/init`.

## Marker table

| Boundary | Observed | Evidence |
|---|---:|---|
| U-Boot and `Starting kernel` | yes | `boot1.serial.log` |
| Asterinas `init-spawned` | yes | serial line 190 |
| First-process stdio ready | yes | serial line 194 |
| Stage1 `step=start mode=systemd` | yes | serial line 199 |
| Fixed `/dev/vdb` selection and probe | yes | serial lines 201–204 |
| Root mount, `/dev`, proc, sysfs, run, tmp, chroot, chdir | yes | serial lines 205–222 |
| Stage1 `handoff-enter action=exec` | yes | serial line 223 |
| systemd PID 1 banner | yes | serial line 226 |
| `systemd-sysctl.service` | failed | serial line 283: `Failed to start systemd-sysctl.service` |
| `basic.target` completion | no | no `Reached target Basic System` marker within bound |
| Xorg / Firefox | no | desktop and Firefox units were masked |

The guest reached PID 1 and then remained in systemd startup. The last useful
unit-level evidence is the `systemd-sysctl.service` failure; this is now the
next diagnostic boundary. The run did not claim a desktop or Firefox result.

## Artifacts

- output: `/mnt/mydisk/qemu-desktop-systemd-boundary-r128`
- serial SHA-256: `f129051a4addb60be1ec12057e21631fe77a069b78dde401bd0210a5d5e40dce`
- lifecycle SHA-256: `2fcb95d95d867e1f12752537739f19d4916975a7b5b602b3efba94764e991d8f`
- result SHA-256: `9aa42901754e4e38b53cb2a69811be64ecb492ef05132fac1367554dd5893688`
- final qcow2 root SHA-256: `ed112904390443722a866d74c6a3d240959bef5e56092e29c9bdc9202df17036`

## Conclusion

The pre-PID1 ambiguity is closed. The next independent gate is
`desktop-systemd-sysctl-unit`: determine whether the failure is an expected
unsupported-sysctl warning or a blocking unit failure, then continue to the
`basic.target` boundary. Do not return to Firefox capability negotiation yet.
