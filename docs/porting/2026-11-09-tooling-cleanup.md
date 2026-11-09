# Tooling cleanup, 2026-11-09

Removed scripts that no longer had a caller in the code, the Makefile, the
host test suite, or the documentation. Nothing in this page changes a current
workflow: each file was looked up by basename and module path before removal.

Recovery ref (created before the removal commit):

```bash
git show refs/archive/local-2026-11-09-tooling-cleanup:tools/riscv/...
```

## Removed files

| File | Why it was dead |
| --- | --- |
| `tools/riscv/debian/rootfs/desktop_drm_device_access.sh` | M4-era DRM device setup; replaced by `desktop_m3_device_access.sh`, which `build_rootfs.sh` installs |
| `tools/riscv/debian/rootfs/desktop_drm_session.sh` | M4-era session wrapper; the M5 session is `desktop_m5_session.sh` |
| `tools/riscv/debian/rootfs/display_m4_dev_overlay.json` | M4 development overlay; not in any build input list |
| `tools/riscv/nixos/mlock/build_mlock.sh` | One-off builder for the mlock milestone; the milestone itself is not referenced anywhere |
| `tools/riscv/perf/ab-redundant-units.sh` | One-off A/B arm builder for the redundant-unit measurement |
| `tools/riscv/perf/debian/fio_case.sh` | Guest fio case with no runner; no script installs or invokes it |
| `tools/riscv/perf/sample-qemu-io.sh` | One-off host I/O sampler for a single measurement run |
| `tools/riscv/perf/toggle-desktop-session.sh` | One-off mask toggle for the desktop A/B; no caller |
| `tools/riscv/xorg/test_xft_link.c` | One-time static-link sanity check for the Phase-1 libX closure |

## Generated clutter

Also removed from the working tree (all gitignored, none tracked):

- every `tools/**/__pycache__/` directory;
- `qemu.log`, `qemu-serial.log` at the repository root;
- TLA+ `*_TTrace_*` traces and `states/` directories under
  `tools/riscv/concurrency-models/`.

Build output under `target/`, `osdk/target/` and `test/initramfs/build/` is
intentionally kept: it is cheap to keep and expensive to rebuild.

## Archived directories

Four directories moved to `tools/riscv/legacy/`; nothing in the Makefile, the
host test suite, or the current gates referenced them:

| Old path | New path |
| --- | --- |
| `tools/riscv/game/` | `tools/riscv/legacy/game/` |
| `tools/riscv/lvgl/` | `tools/riscv/legacy/lvgl/` |
| `tools/riscv/xfce/` | `tools/riscv/legacy/xfce/` |
| `tools/riscv/compat/` | `tools/riscv/legacy/compat/` |

Paths inside the moved code were adjusted for the extra directory level
(`../../../lib/shell.sh` in the LVGL build, `parents[4]` in the Python root
resolution, four `..` in `xfce_cross_env.sh`, and the `tools.riscv.legacy.compat`
module spelling). Dated commands in `docs/porting/riscv-qemu-desktop.md` and in
the XFCE-M2/M3 reports keep the old paths on purpose; `LAYOUT.md` is the
current answer.

## What was deliberately kept

- `tools/riscv/pidns/`, `nsmatrix/`: the C sources are the reproducible part of
  the namespace reports; the run logs already live in
  `docs/porting/evidence/2026-11-05-namespace-run-logs/`.
- `tools/riscv/wayland/`: still compiled and exercised by
  `tools/riscv/tests/test_wayland_demo.py`.
- `tools/riscv/diagnostics/firefox_checkpoint_baseline.py`: its SHA-256 is
  pinned by the tested firefox experiment, so it cannot carry an edited header.
