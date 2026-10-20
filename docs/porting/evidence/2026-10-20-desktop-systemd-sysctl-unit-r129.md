# desktop-systemd-sysctl-unit r129

## Scope

This boundary used one bounded generic-QEMU run with the same inputs as r128,
then a read-only inspection of the Debian systemd unit graph. No Xorg,
Firefox, physical board, serial write, or kernel change was used.

The run attempted `systemd.mask=systemd-sysctl.service`, but the mask did not
appear in the guest: systemd still started `systemd-sysctl.service`. This is
evidence that the current Asterinas-to-PID1 command-line path cannot be used as
a reliable unit-control mechanism.

## Runtime markers

- Stage1 and systemd startup were reached.
- `systemd-sysctl.service` again reported `Failed to start systemd-sysctl.service`.
- No `basic.target` completion marker appeared within the 120-second bound.
- The desktop and Firefox services were not started.

Artifacts:

- output: `/mnt/mydisk/qemu-desktop-systemd-sysctl-mask-r129`
- serial SHA-256: `f55246bbe6a1accb333c1d94a879e5b43bece6bc5e7da013495d25182458e79f`
- lifecycle SHA-256: `5ee9efbe5afd0b1870c8b14017dd270e729975298fbcf1824491ad9860446ebb`
- result SHA-256: `ad6e5b1e666f6ad05c144ca247d0526c1209362e3fe45ec4ad38b4bce3ff7d4b`

## Static dependency result

The frozen rootfs contains:

- `/lib/systemd/system/systemd-sysctl.service` with
  `Before=sysinit.target` and no `Requires=` or `RequiredBy=` dependency;
- a symlink under `sysinit.target.wants`, which makes it a wanted unit;
- therefore a failed sysctl service is a failed `Wants=` member and should not
  by itself make `sysinit.target` or `basic.target` fail.

The unit's `ConditionPathIsReadWrite=/proc/sys/net/` is also explicit. The
failure is consistent with Asterinas' intentionally partial `/proc/sys`
write support, but this receipt does not claim the exact failing sysctl key.

## Conclusion

`systemd-sysctl.service` is a compatibility warning and a timing contributor,
not proven to be the hard dependency that blocks the desktop. The next gate is
`desktop-systemd-sysinit`: capture the first `sysinit.target` completion or
identify the next failed/timed-out unit. A rootfs `/dev/null` mask is not
needed yet.
