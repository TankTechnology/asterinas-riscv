# M5/browser-web cleanup r74

- Gate: `m5-browser-legacy-cleanup`
- Scope: second static reference audit and removal of two orphaned diagnostic
  files (the earlier r73 orphan test remains recorded in the same manifest).
- Runtime scope: none; no QEMU, Firefox, serial, board, image build, or reboot.

## Removed

1. `tools/riscv/debian/rootfs/firefox_startup_profile.py`
2. `tools/riscv/tests/test_firefox_debug_tool.py`

Both files were clean before removal. The scanner and literal search found no
Makefile, overlay, builder, E1–E5 gate, or control-chain reference. The only
references were the pair themselves and an obsolete README command, which was
removed in the same change.

## Retained boundary

The browser-web overlay and M5/M6/M7 modules remain because they are still
reachable from `build_rootfs.sh`, `build_stage1.sh`, legacy Megrez adapters,
or dirty worktree files. `browser_m5_marionette_gate.py` remains reachable
from the current E3 Firefox scene. Frozen cross-build helpers and the
documented GDB diagnostic script were not deleted.

## Required verification

The post-deletion checks completed as follows:

| Check | Result |
| --- | --- |
| Reference scanner plus deletion-manifest validation | pass; 210 candidates |
| Literal repository re-scan for deleted paths | pass; no active code/Makefile/overlay/test reference |
| `python3 -m unittest tools.riscv.tests.test_refactor_reference_scan -q` | pass; 3 tests |
| `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests tools/riscv` | pass |
| `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh` | pass |
| JSON validation and `git diff --check` | pass |
| `make test_riscv_debian_rootfs_unit` in `asterinas-qemu-gl-runtime` | pass; 329 tests |
| `bash tools/riscv/firefox_fast_check.sh` in the same container | pass; 329 tests; `FIREFOX_FAST_CHECK_PASS` |
| `make test_riscv_firefox_daily_use_physical_unit` in the same container | pass; 373 tests, 3 skipped |

The only remaining search hits for the deleted basenames are the deletion
manifest and audit documents (plus an old worktree commit message); none is an
executable or build input.
