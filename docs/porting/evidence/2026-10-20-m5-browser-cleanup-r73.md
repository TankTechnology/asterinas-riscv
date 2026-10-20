# M5/browser-web cleanup r73

- Gate: `m5-browser-legacy-cleanup`
- Scope: static reference audit plus removal of one orphan test module.
- Runtime scope: none; no QEMU, Firefox, serial, board, image build, or reboot.

## Changed files

Removed:

- `tools/riscv/tests/test_debian_browser_m5.py` — clean test module with no
  importer, Makefile entry, overlay reference, or E1–E5 management-chain use.

Restored and retained:

- `tools/riscv/tests/test_debian_browser_m5_runtime_gate.py` — the daily-use
  unit `test_existing_session_skips_drain_for_a_command_that_never_sent`
  imports this module dynamically.  The first test run exposed that dependency;
  the file was restored from `HEAD` before the final run.

Deferred:

- `browser_m5_marionette_gate.py` remains an E3 dependency of
  `firefox_drm_scene_gate.py`.
- `browser_web_*`, `desktop_m5_*`, `desktop_m6_*`, and `desktop_m7_*` remain
  referenced by legacy profile builders/adapters or have dirty changes.  They
  require a separate builder migration before deletion.

## Verification

| Check | Result |
| --- | --- |
| Reference scanner + deletion manifest validation | pass; 212 candidates |
| `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests tools/riscv` | pass |
| `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh` | pass |
| JSON validation and `git diff --check` | pass |
| `make test_riscv_debian_rootfs_unit` in `asterinas-qemu-gl-runtime` | pass; 329 tests |
| `bash tools/riscv/firefox_fast_check.sh` in `asterinas-qemu-gl-runtime` | pass; 329 tests (`FIREFOX_FAST_CHECK_PASS`) |
| `make test_riscv_firefox_daily_use_physical_unit` in the same container | pass; 373 tests, 3 skipped |

The initial host-side rootfs test attempt failed only because the host had no
`qemu-img`; rerunning in the repository's persistent container used its pinned
`/usr/local/qemu/bin/qemu-img` and passed.  No source or management-chain
behavior was changed to work around that environment difference.
