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

## Status

Landed now: the catalog, the runner's selection/order/target logic, `rt
selftest --list` and `--dry-run`, and the unit tests that pin the catalog.

Next stage (needs one of: a rebuilt Debian image, or the board):

1. add the guest bundle to the rootfs build and the runtime digest;
2. implement the host collector and the board/QEMU session adapter;
3. migrate `console`, `block`, `net`, `power` (multi-user boot);
4. migrate `display`, `input`, `browser`, `apps` (graphical boot);
5. migrate `drm` (separate device stack);
6. delete the gate modules, Makefile targets and `rt gate` registry as each
   subsystem lands.

Until step 6 the `rt gate` entries remain as transitional aliases; no new
gate scenario should be added.
