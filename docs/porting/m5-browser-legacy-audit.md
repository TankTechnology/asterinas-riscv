# M5/browser-web legacy audit

Date: 2026-10-19

This audit uses the E1–E5 management-chain roots, the Makefile test roots, and
the repository reference scanner. A filename is removable in this pass only if
it has no code, build, overlay, Makefile, or selected-test reference and is not
dirty in the worktree.

## Deletion list for this pass

The following files satisfy that rule. They are unit tests for retired browser
workloads; their implementation modules are not removed by this change.

| Path | Evidence | Reason |
| --- | --- | --- |
| `tools/riscv/tests/test_debian_signed_sources.py` | scanner: 0 active references; clean | superseded source-signing test, not selected by an E1–E5 target |
| `tools/riscv/tests/test_browser_composite_manifest.py` | scanner: 0 active references; clean | retired composite browser manifest test |
| `tools/riscv/tests/test_browser_interaction_perf.py` | scanner: 0 active references; clean | retired long browser interaction-latency test |
| `tools/riscv/tests/test_browser_performance_provenance.py` | scanner: 0 active references; clean | retired browser provenance test |
| `tools/riscv/tests/test_browser_system_time.py` | scanner: 0 active references; clean | retired browser/system-time test |
| `tools/riscv/tests/test_browser_workload_contract.py` | scanner: 0 active references; clean | retired composite workload contract test |

## Retained active dependencies

* `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py` is imported by
  `firefox_drm_scene_gate.py` and copied by both rootfs and Stage1 builders;
  it remains an E3 dependency.
* `browser_m5.py`, its local media fixture, launcher, observers, and the
  browser-m5 runtime test remain tied to the current builder and/or Firefox
  scene. They are not independently removable.
* `desktop_m5_*`, `desktop_m6_*`, and `desktop_m7_*` remain imported by legacy
  Megrez adapters and their tests. Removing them requires a separate adapter
  migration, so they are outside this bounded pass.

## Deferred (do not delete in this pass)

These paths either have live legacy build/test references or pre-existing dirty
changes. Deleting them now would leave a dangling builder/overlay import or
discard uncommitted work:

* `tools/riscv/debian/rootfs/browser_web_*.py`, `browser_web_*.sh`,
  `browser_web_*.service`, and `browser_web_diagnostic.conf`;
* `tools/riscv/debian/rootfs/browser_web_dev_overlay.json`;
* `tools/riscv/debian/rootfs/build_rootfs.sh` and `build_stage1.sh`;
* `tools/riscv/tests/test_debian_browser_web.py`;
* `tools/riscv/tests/test_debian_m5_network.py`;
* dirty M5 launch/evidence files and the Megrez adapters that still import
  `desktop_m5_*`/`desktop_m6_*`/`desktop_m7_*`.

## Follow-up candidate review

The reference scanner also reports the following zero-active-reference paths.
They are not all removable: a zero count does not account for dynamic paths,
dirty work, or an untracked artifact supplied by an earlier experiment.

| Path | Decision | Reason |
| --- | --- | --- |
| `tools/riscv/debian/rootfs/desktop_m9_software_evidence.sh` | delete | Clean tracked implementation paired with the already removed M9 gate; no code, Makefile, overlay, test, or management-chain caller. |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | retain/defer | Existing dirty change and historical overlay entry points; deleting would discard work and may break dynamic overlay selection. |
| `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` | retain/freeze | Untracked cross-build artifact; the official-package decision freezes it, but does not authorize deleting an untracked artifact. |
| `tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py` | retain/freeze | Untracked cross-build artifact; same freeze rule. |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | retain | Untracked service file; no evidence that it is owned by this cleanup pass. |
| `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` | retain | It has a documented diagnostic invocation in `rootfs/README.md`; removal would leave a broken documented tool. |
| `tools/riscv/tests/test_debian_desktop_m4_x11_gate.py` | defer | Dirty and still documents the X11 contract. |
| `tools/riscv/tests/test_debian_m5_network.py` | defer | Dirty; legacy adapter references remain and the file is part of the current physical-unit test surface. |
| remaining zero-reference tests | retain | Untracked or outside the M5/browser-web management chain; no deletion authority was established for them. |

Only the first row is removed by this follow-up. This keeps the deletion
bounded and leaves the management-chain roots intact.

No QEMU, Firefox, board, or image build is part of this audit.

## Validation

* `python3 tools/riscv/refactor_reference_scan.py ... --verify`: passed;
  the historical tracked-tree report had 214 candidates; the latest scan has
  574 because it also inventories untracked frozen artifacts.  The latter is
  an inventory only and does not authorize deleting those artifacts.
* `make test_riscv_debian_rootfs_unit`: passed, 327 tests (latest run).
* `bash tools/riscv/firefox_fast_check.sh`: passed, 327 tests and static checks
  (latest run).
* `PYTHONPATH=tools/riscv python3 -m unittest
  tools.riscv.tests.test_refactor_reference_scan
  tools.riscv.tests.test_megrez_firefox_daily_use -q`: passed, 19 tests.
* `py_compile`, `bash -n`, and `git diff --check`: passed.
* `make test_riscv_firefox_daily_use_physical_unit`: passed, 371 tests
  (latest host-only run; despite the target name it does not access the board).
  No physical-board claim is made here.
