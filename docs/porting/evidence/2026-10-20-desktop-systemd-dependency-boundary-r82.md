# M4 systemd dependency boundary (r82)

- Gate: `desktop-systemd-dependency-boundary`
- Status: `code-pass`; no new QEMU, Firefox, serial, or physical-board run.

## Finding

The r80 serial receipt reached Stage1 handoff and the systemd banner, but no
desktop service marker appeared before the bounded deadline:

`/mnt/mydisk/qemu-desktop-m4-r80-cow-stage1-120b/desktop-m4.serial.log`

The M4 unit was ordered after `systemd-logind.service` and
`systemd-udevd.service`.  Those optional services are known to remain in a
notify/ioctl boundary on this kernel, so the desktop could be held before its
bounded device-access helper ran.

## Change

For generation `m4`, `build_rootfs.sh` now emits only:

```ini
After=local-fs.target
Wants=
```

The M4 helper remains responsible for bounded device discovery and input
setup.  M3/M5 profiles retain their existing service dependencies; PAM/TTY
directives remain absent from M4.

## Verification

| Check | Result |
| --- | --- |
| TDD red test before source change | PASS (old logind/udev dependency detected) |
| M4 builder contract after change | PASS |
| `make test_riscv_debian_rootfs_unit` | PASS, 329 tests |
| Python compile, shell syntax, `git diff --check` | PASS |

The next runtime result must use a rootfs materialized from this source state;
the r80 image remains a historical failure and is not reclassified.
