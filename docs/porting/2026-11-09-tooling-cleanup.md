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

Build output under `target/`, `osdk/target/` and `test/initramfs/build/` was
left alone during the first pass and removed in the minimalism pass below.

## Historical demos

`tools/riscv/game/`, `lvgl/`, `xfce/` and `compat/` were first moved to
`tools/riscv/legacy/` and then deleted outright on the same day. None of them
was referenced by the Makefile, the host test suite, or the current gates.
The pre-cleanup tree still has them at their original paths through the
recovery ref.

## Milestone reports

The dated milestone reports that lived next to their code in `tools/riscv/`
were deleted: `xorg/BROWSER-M{1,4,5,6,7,8,9,10,12}-report.md`,
`xorg/GTK-M{1,2,3}-report.md`, `xorg/KEYBOARD-M1-report.md`,
`xorg/UI-EVAL-M1-report.md`, and the `systemd/*-report.md` set
(`SYSTEMD-M*`, `SYSTEMD-BOOT-M*`, `SYSTEMD-DESKTOP-M1`, `GTK-INTERACTIVE-M1`,
`NIXOS-STAGE2-M1`, `DEMO-M1`). Comments in `build_systemd_desktop.sh`,
`build_systemd_boot.sh`, `render_matrix_net.sh`, `build_xorg_initramfs.sh` and
`README-DEMO.md` no longer point at them. The LTP reports stay: `test_ltp_gate`
pins `ARCH-RISCV64-M1-report.md`.

## NixOS milestone tree

The `track/nixos` milestone experiment was deleted from `tools/riscv/nixos/`:
the FOUNDATION, M2–M9, N1/N3, PM1, mlock, shm, etexec and audio directories,
their smoke scripts, and the POLISH/TRACK-ADMISSION reports. The same change
removed the three legacy gates (`fm3-gate.sh`, `fm4-gate.sh`, `pm1-gate.sh`),
`nixos_track_audit.py`, the `test_nixos_m7_assets` and `test_nixos_track_audit`
modules, the systemd ALSA trio (`build_systemd_alsa.sh`,
`boot_systemd_alsa.py`, `gate_alsa.sh`), and `ACCELERATION.md`.

Kept because current code still uses them:

- `tools/riscv/ltp/guest/` (moved from `nixos/ltp/`) and
  `tools/riscv/ltp/build_busybox.sh`: the active LTP gate invokes
  `build_ltp.sh` and the tests require the busybox builder.
- `tools/riscv/systemd/` core (`boot_systemd_desktop.py`, `units/`,
  `build_systemd_desktop*.sh`): the xorg render matrices and `demo-all.sh`
  drive these.
- `tools/nixos/`: a separate Nix image toolchain covered by
  `test_nixos_riscv_preflight`.

## Build caches

The ignored build trees were removed to reclaim disk: `target/` (7.7 GiB),
`osdk/target/` (3.0 GiB), `test/initramfs/build/` (551 MiB) and
`target-ubuntu/`. They are reproducible; the next build recreates them.

## Superseded boot drivers

Six one-off drivers with no caller were deleted as well:
`systemd/boot_backspace_test.py` (the keyboard milestone is covered by the
kernel line-discipline fix), `systemd/boot_gtk_interactive.py` (superseded by
the browser-web session), `systemd/boot_systemd_desktop_vnc.py` (superseded by
`boot_systemd_desktop.py`), `systemd/gate_desktop.sh`, `systemd/gate_m2.sh`,
and `xorg/xwm_reparent.c`.

## Single entry point

The tooling was reshaped after the Linux `tools/` model: one executable with
subcommands, implementation in domain packages.

- `tools/riscv/rt <command>` is the only public entry point; `rt --list` prints
  the command set. It sets `PYTHONPATH` itself, so commands run from any
  directory or container.
- The loose top-level Python modules moved into `ltp/`, `perf/`,
  `debian/rootfs/`, `drm/`, `qemu/`, `board/`, `megrez/`, and
  `megrez/desktop/`. The ten top-level shell scripts moved into `ktest/`,
  `ltp/`, `board/`, `qemu/`, `systemd/`, and `debian/rootfs/`.
- `tooling_surface.json` now caps top-level shell scripts at zero. The only
  top-level Python modules left are `host_test_runner.py` and
  `physical_graphics_qemu_gate.py`, the latter because a SHA-256-pinned
  experiment imports it by its top-level name.
- The Makefile now invokes `tools/riscv/rt <command>` for every tool target.

## Retired gates

Three milestone gates whose scenarios the browser-web path replaced were
deleted along with their tests and Makefile targets:
`browser_m5_qemu_gate` (the M5 offline probe is covered by the browser-web
gate), `desktop_m9_software_gate` (the M8/M9 software matrix is covered by
the daily-use path), and the systemd-m2 target (the cross-built systemd image
is gone; `orchestrate_systemd_m2_gate` remains as the boot helper used by the
physical-graphics gate). The M5 milestone tuple stays inlined in the browser-web
test for the non-overlap assertion; the `browser-m5` and
`desktop-m9-software` rootfs profiles themselves remain buildable.

## One-shot diagnostics

Eight probes with no code or test caller were deleted from
`tools/riscv/diagnostics/`: `dhcp_probe.py`, `fb_downsample_guest.py`,
`gdk_pixbuf_probe.py`, `klog_console_probe.c`, `klog_micro_init.sh`,
`startup_cost_probe.c`, `syscall_bench.c`, `sysv_shm_rmid_probe.c`. The
probes still used by tests or by the Megrez desktop baseline remain.

## What was deliberately kept

- `tools/riscv/pidns/`, `nsmatrix/`: the C sources are the reproducible part of
  the namespace reports; the run logs already live in
  `docs/porting/evidence/2026-11-05-namespace-run-logs/`.
- `tools/riscv/wayland/`: still compiled and exercised by
  `tools/riscv/tests/test_wayland_demo.py`.
- `tools/riscv/diagnostics/firefox_checkpoint_baseline.py`: its SHA-256 is
  pinned by the tested firefox experiment, so it cannot carry an edited header.
