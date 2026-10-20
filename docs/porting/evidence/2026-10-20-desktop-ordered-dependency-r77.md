# M4 ordered-unit dependency boundary (r77)

- Gate: `desktop-ordered-unit-dependency`
- Scope: one-variable QEMU diagnosis plus rootfs generator hardening.
- Status: `root-cause-identified`; desktop runtime pass is not claimed.

## Evidence

The current Stage1 archive reached the same `handoff-enter action=exec` and
systemd banner as r75/r76.  Comparing the known working r69 image with the
r73-derived image showed that the latter retained an old
`asterinas-desktop-m4-ordered.service` and an old evidence unit containing:

```ini
After=basic.target asterinas-desktop-m4.service asterinas-desktop-m4-ordered.service
Wants=asterinas-desktop-m4.service asterinas-desktop-m4-ordered.service
```

The retained ordered unit itself waited for `systemd-udevd.service` and
`systemd-logind.service`.  r69 instead contained a retired `/bin/true` alias,
so its graphical target was able to complete.  This explains why r76/r76b
reached systemd but emitted no desktop marker.

## Code change

`configure_desktop()` now removes the legacy ordered unit and its graphical
target link whenever an M4 stage is configured.  The generated M4 service
already uses the dbus-only dependency boundary; the generated evidence unit
owns only that service.

## Verification

| Check | Result |
| --- | --- |
| targeted M4 generator test | PASS |
| `make test_riscv_debian_rootfs_unit` | PASS, 327/327 (second run; first run had one host scheduling timeout) |
| `bash -n` / `compileall` / `git diff --check` | PASS |
| r75 current Stage1 + stale root | systemd banner, no desktop marker |
| r76 corrected service only | systemd banner, legacy evidence dependency still present |
| r76b corrected service fields | no marker within 60s; stale evidence dependency remained |
| r76b at 120s | systemd banner, no target/desktop marker |
| r76c two-file overlay | materialized; one 60s QEMU probe reached the systemd banner but no desktop marker |

Outputs for the bounded probes are retained under:

- `/mnt/mydisk/qemu-desktop-m4-r75-stage1`
- `/mnt/mydisk/qemu-desktop-m4-r76-stage1`
- `/mnt/mydisk/qemu-desktop-m4-r76b-stage1`
- `/mnt/mydisk/qemu-desktop-m4-r76b-stage1-120`
- `/mnt/mydisk/qemu-desktop-m4-r76c-rootfs`
- `/mnt/mydisk/qemu-desktop-m4-r76c-stage1`

The r76c rootfs is bound to the following hashes:

```text
debian-root.ext2                  e661a64c87b802c26a7543d441ed6397a7406027ac95d337cd27744bd7e8d2f0
rootfs-manifest.json              e26ea88d2e7fd1632e965720c202d3195ee5732d1a63451896f69b98d8452d6c
dev-overlay-manifest.json         1483c6d6272d55d29a20be5f8eb8cfa8ced13e4a15f4fb477be6d32f2a203cd3
```

The r76c runtime receipt is `result.json` with `passed=false` and
`reason="protocol"`.  Its serial log contains the complete boot and rootfs
handoff through `DEBIAN_STAGE1_PROGRESS step=handoff-done action=exec`, then
the systemd banner, but no `DEBIAN_DESKTOP_M4_*` marker before the 60-second
deadline.  This closes the stale-unit hypothesis as a necessary fix, but does
not yet prove the service boundary at runtime; the next blocker is the
post-handoff systemd activation path.

The follow-up fb0 metadata probe and its 120-second runtime result are
recorded in [2026-10-20-desktop-fbdev-stat-boundary-r78.md](2026-10-20-desktop-fbdev-stat-boundary-r78.md).

No Firefox scene, physical board, serial write, or hardware operation was run.

## Next gate

Do not start E3 Firefox yet.  The next bounded gate is a service-activation
probe that records systemd's target/job state after handoff (without adding
another long scene).  A desktop marker would close the service boundary; only
then may E3 Firefox be considered.
