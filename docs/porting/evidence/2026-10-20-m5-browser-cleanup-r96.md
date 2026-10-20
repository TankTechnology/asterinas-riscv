# M5/browser-web cleanup r96

Date: 2026-10-20
Gate: `m5-browser-legacy-cleanup`

## Deletions

* `tools/riscv/xorg/render_matrix_net.sh` — obsolete BROWSER-M10 NetSurf
  multi-site matrix; no current source, Makefile, profile, E1--E5, or
  management-chain caller.
* `tools/riscv/xorg/tls_cert_matrix.sh` — obsolete BROWSER-M12 NetSurf/curl
  certificate matrix; no current source, Makefile, profile, E1--E5, or
  management-chain caller.

The deletion list and the retained/deferred paths are recorded in
`docs/porting/m5-browser-legacy-audit-r96.md`. `browser-web` builder inputs and
`browser_m5_marionette_gate.py` were retained because they are selected by the
rootfs builder or imported by the current E3 Firefox probe.

## Verification

* `python3 tools/riscv/refactor_reference_scan.py --repository . --root
  tools/riscv/debian/rootfs --tests tools/riscv/tests --makefile Makefile
  --output /tmp/refactor-reference-final-r96.md --verify
  docs/porting/refactor-deletions.json` — scanned 210 candidates, exit 0.
* `python3 -m compileall -q tools/riscv` — pass.
* `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh
  tools/riscv/xorg/*.sh` — pass.
* `git diff --check` and `python3 -m json.tool
  docs/porting/refactor-deletions.json` — pass.
* Focused browser/management unit suite — 196 tests, all pass.
* `tools/riscv/firefox_fast_check.sh` — 334 tests, all pass; emits
  `FIREFOX_FAST_CHECK_PASS`.

No QEMU guest or hardware run was introduced by this cleanup.
