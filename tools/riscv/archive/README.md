# Archived RISC-V research material

This directory holds historical material removed from the active `tools/riscv`
surface. Archived code is not imported by the current build, tests, Makefile
targets, or documented product path. Keep evidence files in
`docs/porting/evidence/`; they are not archive candidates.

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
