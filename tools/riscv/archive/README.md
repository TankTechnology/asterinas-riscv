# Archived RISC-V research material

This directory holds historical reports and evidence removed from the active
`tools/riscv` surface. Archived code is not imported by the current build,
tests, Makefile targets, or documented product path. Keep evidence files in
`docs/porting/evidence/`; they are not archive candidates.

## Archived implementation removed from git (2026-10-28)

The archived implementation files (Python, shell, C, Nix, and generated
auxiliary configuration) were removed from the repository to stop inactive code
from counting toward the maintained RISC-V tool surface. The complete tree was
saved in the operator's local backup before removal, and the implementation
remains recoverable from the git history before this change. Reports, images,
logs, and evidence in this directory are retained.

## systemd demo (archived 2026-10-28)

The pre-Debian systemd + NixOS full-system demo (`demo-all.sh`,
`README-DEMO.md`, and the `tools/riscv/systemd/` implementation) was removed
from the active surface. Its reports are retained here. The last implementation
is available from commit `ce65773aa`:

```bash
git show ce65773aa:tools/riscv/demo-all.sh
git show ce65773aa:tools/riscv/systemd/boot_systemd_desktop.py
```

The current Megrez desktop path is the Debian `browser-web` image documented in
`docs/porting/board-image-current.md`.

## M6/M7 guest browser evidence (archived 2026-10-28)

The Netsurf-era M6 JavaScript probe, M7 Baidu page services, their host gates,
and their unit tests moved here after `browser-web` became the only browser
image. The current browser path is `browser_web_evidence.sh` and
`browser_web_marionette_gate.py`; GMAC now exposes only `network`, `desktop`,
and `firefox` targets.

## Firefox/dmesg diagnostics (archived 2026-10-28)

The one-off Firefox actor, transport, dmesg, klog, and log-profile diagnostics
and their host tests moved to `archive/diagnostics/`. The current Firefox
acceptance path is the browser-web QEMU/physical daily-use gate; the full
`make test_riscv_host` runner replaces the old `firefox_fast_check.sh`.

## Standalone debug probes (archived 2026-10-28)

The one-off Megrez probe, dual-host probe, SDHCI probe, Firefox debug tool,
thread-PC/video samplers, and QEMU GDB sampling helpers moved to
`archive/debug/`. The keep-set starts from the browser-web, board-install,
QEMU/U-Boot, debug-console, and physical-graphics product paths; a diagnostic
script is retained only while a current Makefile target or product path calls it.

## Other archived tracks (2026-10-28)

- `nixos/`: the old NixOS RISC-V research track, except the active
  `tools/riscv/nixos/ltp/` and `tools/riscv/nixos/build_busybox.sh` leaves used
  by the LTP gate.
- `xorg/`: the pre-Debian Xorg/Netsurf build and report tree, except the active
  `gen_tls_certs.py` and `tls_cert_server.py` helpers kept in
  `tools/riscv/xorg/` for the M5 network QEMU gate.
- `xfce/`, `wayland/`, `lvgl/`: superseded custom desktop demos.
- `nsmatrix/`, `pidns/`, `reports/`: historical namespace probes, logs, KE
  reports and the retired NixOS/desktop reports.
- `eic7700_isolation.sh`: historical QEMU isolation helper.

Archived code is not counted by `tooling_surface.json`; the guard excludes the
`archive/` prefix from active production counts.
