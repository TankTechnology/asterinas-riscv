# M5/browser-web cleanup r72

- Gate: `m5-browser-legacy-cleanup`
- Scope: static reference audit and removal of files with no management-chain caller.
- Runtime scope: none; no QEMU, Firefox, serial, board, image build, or reboot.
- Reference inventory at the time of the bounded cleanup: `docs/porting/refactor-reference-report.md`
  (`213` candidates).  A current rerun also scans untracked frozen artifacts and
  reports `574` candidates; those are not deletion authorization.
- Deletion manifest: `docs/porting/refactor-deletions.json` (all entries have
  `references_checked: true`; seven tracked files are now removed in the
  current cleanup series).

## This pass

Removed:

- `tools/riscv/debian/rootfs/desktop_m9_software_evidence.sh` — the retired M9
  evidence implementation; its gate and test are already removed and the
  scanner found no active code, Makefile, overlay, test, or management-chain
  caller.

The six browser tests removed in the preceding bounded pass remain recorded in
the same manifest and audit. No M4/M5/Megrez root, overlay, launcher, or
management-chain implementation was removed here.

Deferred candidates are recorded in
`docs/porting/m5-browser-legacy-audit.md`. In particular, the dirty
`browser_web_dev_overlay.json`, dirty M4/M5 tests, documented
`firefox_gdb_probe.sh`, and untracked cross-builder files were retained.

## Verification

| Check | Result |
| --- | --- |
| `make test_riscv_debian_rootfs_unit` | `327` passed (latest rerun) |
| `bash tools/riscv/firefox_fast_check.sh` | passed (`FIREFOX_FAST_CHECK_PASS`, latest rerun) |
| `make test_riscv_firefox_daily_use_physical_unit` | `371` passed (latest host-only rerun) |
| `refactor_reference_scan.py --verify` | passed; `574` current candidates, including untracked frozen artifacts |
| targeted `py_compile` | passed |
| targeted `bash -n` | passed |
| `git diff --check` | passed |

This receipt does not claim E3 Firefox WebRender or E5 physical-board success.
