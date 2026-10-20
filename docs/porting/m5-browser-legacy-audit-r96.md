# M5/browser-web legacy audit r96

Date: 2026-10-20

This pass audits the remaining executable matrix helpers under `tools/riscv/xorg`.
The search covers Makefile targets, rootfs profiles and builders, E1--E5 checks,
and management-chain adapters. Historical reports are retained as evidence but
do not count as active callers.

## Candidate deletion list

| Path | Active callers | Decision | Reason |
| --- | --- | --- | --- |
| `tools/riscv/xorg/render_matrix_net.sh` | none outside its own comments and BROWSER-M10 reports | delete | obsolete NetSurf multi-site matrix; no current profile, Makefile, E1--E5, or management-chain entry |
| `tools/riscv/xorg/tls_cert_matrix.sh` | none outside its own comments and BROWSER-M12 report | delete | obsolete NetSurf/curl certificate matrix; no current profile, Makefile, E1--E5, or management-chain entry |
| `tools/riscv/xorg/net_validate.sh` | current legacy Xorg helpers and reports | retain | still used by the remaining legacy harnesses; deleting it would break active scripts outside this pass |
| `tools/riscv/xorg/build_netsurf.sh`, `build_pcmanfm.sh`, `build_xorg_initramfs.sh` | legacy helper graph | retain | dependency graph was not migrated in this pass; no safe file-only deletion |

The `browser-web` rootfs scripts and `browser_m5_marionette_gate.py` remain
retained: the builder and E3 Firefox probe select them dynamically. Historical
BROWSER-M5--M12 reports remain retained for traceability.

## Verification boundary

The two deleted scripts have no callers in source, Makefiles, profiles, tests,
or management adapters. Their only remaining textual mentions are historical
documentation and the self-description of the deleted matrix. This pass does
not change rootfs construction or E1--E5 runtime behavior.
