# Subsystem selftests, 2026-11-09

The per-milestone boot gates are being replaced by Linux-style subsystem
selftests: one boot path, a catalog of subsystems, and an ordered set of
cases validated inside the same booted guest.

## Model

```
rt selftest [subsystem ...]        # host runner
  ├─ prepare one boot medium
  ├─ boot once to the highest target the selection needs
  ├─ run each subsystem's cases in catalog order inside the guest
  └─ collect `SELFTEST <subsystem> <case> PASS|FAIL` lines + artifacts
```

The host decides the boot target: `multi-user` when only console, block, net
or power are selected; `graphical` as soon as display, input, browser, apps or
drm is requested. A run never boots once per case.

The guest side is a selftest bundle (`/usr/lib/asterinas/selftests/run.sh`
plus one script per subsystem) selected by a kernel command-line/env value.
Board and QEMU share the same bundle; only the session transport differs
(serial bridge vs U-Boot boot).

## Catalog

| Subsystem | Target | Cases |
| --- | --- | --- |
| `console` | multi-user | identity, clock, tty, sudo |
| `block` | multi-user | rootfs-rw, fsck, space |
| `net` | multi-user | loopback, route, dns, http |
| `power` | multi-user | reboot, poweroff |
| `display` | graphical | xorg, session, screenshot |
| `input` | graphical | keyboard, pointer |
| `browser` | graphical | navigate, render, search |
| `apps` | graphical | terminal, editor, files, archive |
| `drm` | graphical | nodes, render, kms |

The catalog lives in `tools/riscv/selftests/catalog.py`; it is unit-tested and
is the single place that defines what "the system works" means.

## How the current gates map

| Current entry | Becomes |
| --- | --- |
| `rt gate rootfs` | `selftest console block` |
| `rt gate debug-console` | `selftest console` (serial variant) |
| `rt gate desktop` | `selftest display` |
| `rt gate browser-web` | `selftest browser` |
| `rt gate daily-use` | `selftest console net power apps` |
| `rt gate desktop-drm` | `selftest drm` |
| `rt gate graphics-physical` | `selftest display` on the board |
| M5/M6/M7 browser modules | browser cases: navigate, render, search |

## Board run (2026-11-09)

`rt selftest --board console block net` pushed the guest bundle over the
Megrez serial bridge, ran it once on the booted Debian image, and collected
the markers:

```
PASS console  identity   host=asterinas os=Asterinas 13
PASS console  clock      epoch=1794208155
PASS console  tty        tty=/dev/ttyS0
PASS console  sudo       running as root
PASS block    rootfs-rw  root is writable
SKIP block    fsck       root filesystem is mounted; offline image check owns fsck
PASS block    space      avail_kb=331156
PASS net      loopback   tcp connect
PASS net      route      default route present
FAIL net      dns        cannot resolve www.baidu.com
PASS net      http       http=200
SELFTEST total=11 pass=9 fail=1 skip=1 complete=True
```

That first run was before the portal session existed. Two findings:

- **Portal state.** Before the reboot even the gateway dropped unicast
  (DHCP and the default route were fine); after a reboot the board reached the
  gateway and portal again, then the recorded board-side `srun` login
  succeeded with `login: ok` (the strict checker exits 1 on the portal's
  `"res":"ok"`, so connectivity was the confirmation). The rerun with the
  authenticated network gives `PASS net` 4/4 and the full run below.
- **Clock gap.** The image did not ship a clock service, so after a reboot the
  clock was at 2026-04-13. The `clock` case compares against the host epoch
  (`SELFTEST_EXPECT_EPOCH`), and the run below used `date -s @<host epoch>`.
  This is now closed: the image ships `systemd-timesyncd` and the kernel
  implements its needs (see [timesync](2026-11-09-timesync.md)). After a normal
  reboot the same case reports `PASS console clock epoch=... delta=8s` with no
  manual step.

```
PASS console  identity   host=asterinas os=Asterinas 13
PASS console  clock      epoch=1794208985 delta=2s
PASS console  tty        tty=/dev/ttyS0
PASS console  sudo       running as root
PASS block    rootfs-rw  root is writable
SKIP block    fsck       root filesystem is mounted; offline image check owns fsck
PASS block    space      avail_kb=325104
PASS net      loopback   tcp connect
PASS net      route      default route present
PASS net      dns        resolved www.baidu.com
PASS net      http       http=200
SELFTEST total=11 pass=10 fail=0 skip=1 complete=True
```

The `http` case accepts `--http-url`; the run above points it at the host's
18080 server. The `route` case prefers `ip route` because Asterinas does not
implement `/proc/net/route`.

## Status and next stage

Landed: the catalog, the runner (plan, `--list`, `--dry-run`, and `--board`),
the guest bundle for console/block/net/power, and the unit tests for the
catalog and marker parser. The last board run, after the timesync kernel
landed, is `total=11 pass=10 fail=0 skip=1` (`delta=8s` on the clock case).

Next:

1. add the guest bundle to the rootfs build and the runtime digest;
2. implement the QEMU session adapter (the graphical target);
3. migrate `display`, `input`, `browser`, `apps` from the host-driven gates;
4. migrate `drm` (separate device stack);
5. delete the gate modules, Makefile targets and `rt gate` registry as each
   subsystem lands.

Until step 5 the `rt gate` entries remain as transitional aliases; no new
gate scenario should be added.
