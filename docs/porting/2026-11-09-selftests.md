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
| `net` | multi-user | loopback, slirp, dns |
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

The DNS failure is environmental: the campus portal was not authenticated in
that run, so the configured resolvers (223.5.5.5, 114.114.114.114) were
unreachable. The `http` case was validated against a board-local endpoint;
the same case accepts `--http-url` for the host or an external endpoint once
the portal is up. The `route` case prefers `ip route` because Asterinas does
not implement `/proc/net/route`.

## Status and next stage

Landed: the catalog, the runner (plan, `--list`, `--dry-run`, and `--board`),
the guest bundle for console/block/net/power, and the unit tests for the
catalog and marker parser.

Next:

1. add the guest bundle to the rootfs build and the runtime digest;
2. implement the QEMU session adapter (the graphical target);
3. migrate `display`, `input`, `browser`, `apps` from the host-driven gates;
4. migrate `drm` (separate device stack);
5. delete the gate modules, Makefile targets and `rt gate` registry as each
   subsystem lands.

Until step 5 the `rt gate` entries remain as transitional aliases; no new
gate scenario should be added.
