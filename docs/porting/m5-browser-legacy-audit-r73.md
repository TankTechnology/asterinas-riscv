# M5/browser-web legacy audit r73

Date: 2026-10-20

This is a static, host-only audit.  The E1–E5 management roots are
`firefox_fast_check.sh`, the E1–E5 Makefile target, `desktop_m4_gate.py`,
`firefox_drm_scene_gate.py`, the recovery gates, and the control-chain tests.
The audit treats a literal code, Makefile, overlay, or test reference as an
active reference.  A dirty file is never removed in this pass.

## Deletion list (confirmed before removal)

| Path | Checks | Decision |
| --- | --- | --- |
| `tools/riscv/tests/test_debian_browser_m5.py` | clean; `git grep` finds no importer, Makefile target, or E1–E5 reference; scanner finds no active caller | delete |

`test_debian_browser_m5_runtime_gate.py` was initially listed, but a complete
unit run found a dynamic import from
`test_browser_daily_use_gate.py::test_existing_session_skips_drain_for_a_command_that_never_sent`.
It is therefore restored and retained below.

These are orphaned M5 test modules.  They are not collected by any current
Makefile target, and deleting them does not remove a builder, overlay, service,
or management-chain implementation.

## Retained management-chain dependencies

* `browser_m5_marionette_gate.py` is retained because
  `firefox_drm_scene_gate.py` imports it for the current E3 protocol client.
* `firefox_drm_scene_gate.py`, its scene/service, `desktop_m4_*`, gate protocol,
  and recovery/control-chain modules are retained because they are E1–E5
  roots or are copied by the active `desktop-m4-firefox` path.
* `build_rootfs.sh` and `build_stage1.sh` are retained.  They still contain
  profile selection and stage1 publication logic; changing those branches is
  a separate migration and is not safe to infer from filename matching.

## Deferred legacy candidates

The following remain outside this bounded deletion because they have an active
builder/overlay/import reference or an existing dirty change:

* `browser_web_*.py`, `browser_web_*.sh`, `browser_web_*.service`, and
  `browser_web_diagnostic.conf` are referenced by the browser-web builder or
  the dirty `test_debian_browser_web.py`.
* `desktop_m5_*`, `desktop_m6_*`, and `desktop_m7_*` are referenced by
  `build_rootfs.sh` and Megrez legacy adapters.
* `browser_m5.py`, the browser fixture/launcher/observers, and
  `browser_m5_qemu_gate.py` have legacy test or builder references.
* `test_debian_browser_m5_runtime_gate.py` is retained because the daily-use
  adapter imports it inside a test method.
* Dirty files (`browser_web_dev_overlay.json`, `browser_web_qemu_gate.py`,
  `browser_web_firefox.sh`, `browser_web_evidence.sh`,
  `browser_m5_marionette_gate.py`, `browser_m5_firefox.sh`,
  `desktop_m5_network_evidence.sh`, `desktop_m5_session.sh`, and the dirty
  legacy tests) are retained to avoid discarding uncommitted work.

No QEMU, Firefox, serial, board, image build, or reboot is part of this audit.
