# M4 service dependency boundary (r76)

- Gate: `desktop-m4-service-dependency-boundary`
- Scope: rootfs generator and host/unit tests only; no QEMU, Firefox, network,
  serial writes, hardware access, or physical-board operation.
- Status: `pass` for the static contract; E3 runtime remains open.

## Root-cause evidence

The r75 short boot used the rebuilt Stage1 archive and reached systemd, but no
`ASTERINAS_DESKTOP_M4_*` marker appeared before the 60-second deadline.  The
M4 unit generator still emitted `After=local-fs.target dbus.service
systemd-udevd.service systemd-logind.service` and requested both services.
This made unsupported udev/logind probing a hard prerequisite for the desktop
service even though Stage1 supplies the fixed `/dev` tree and M4 has no
PAM/TTY handoff.

## Change

For generation `m4`, `build_rootfs.sh` now emits:

```ini
After=local-fs.target dbus.service
Wants=dbus.service
```

udev and logind remain observable to the evidence service, but cannot prevent
the deterministic M4 service from reaching its own bounded markers.  M5's
existing online-specific dependency policy is unchanged.

## Verification

| Check | Result |
| --- | --- |
| targeted M4 unit-generation test | PASS |
| `make test_riscv_debian_rootfs_unit` | PASS, 327/327 |
| `bash tools/riscv/firefox_fast_check.sh` | PASS, 327 tests (`FIREFOX_FAST_CHECK_PASS`) |
| `make test_riscv_firefox_daily_use_physical_unit` | PASS, 371 host-only tests |
| Python bytecode compilation | PASS |
| shell syntax checks | PASS |
| `git diff --check` | PASS |
| QEMU/Firefox/physical board | NOT RUN |

## Next bounded gate

Materialize a clean M4 rootfs containing this generated unit and run one
service-only QEMU probe with the current Stage1 identity, bounded to 60
seconds.  Do not start the long Firefox scene until an M4 readiness marker is
observed.
