# desktop-systemd-sysusers-mask r133

## Scope

Bounded generic-QEMU diagnostic with no display device, no network, no physical
hardware, and no Firefox/Xorg workload.

## Result

- output: `/mnt/mydisk/qemu-desktop-systemd-sysusers-mask-r133`
- serial SHA-256: `76bbdebfd0da038f7f946346a277ea3bb06b9afc72384919865211123031ce29`
- lifecycle SHA-256: `aef19a0f7be0fc80390cd885fcf8ff02345c602b843dd1b7a5509dec30f6817c`
- result SHA-256: `c6a4f7b4dd429d34998388078b5a5345b3fbb676d2d7ce5a02b122725ce82315`

Observed result: sysusers and tmpfiles-dev were masked, but sysctl still failed and sysinit did not complete.

## Conclusion

This control run does not close sysinit; sysusers and tmpfiles-dev were masked, but sysctl still failed and sysinit did not complete. It supports the r135 isolation and must not be treated as a Firefox or desktop result.
