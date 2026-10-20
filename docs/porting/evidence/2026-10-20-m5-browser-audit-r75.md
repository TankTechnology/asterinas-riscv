# M5/browser audit r75

- Gate: `m5-browser-legacy-audit`
- Result: `passed=true` (audit completed; no additional safe deletion)
- Candidate list: `docs/porting/m5-browser-legacy-audit-r75.md`
- Prior confirmed deletions: `docs/porting/evidence/2026-10-20-m5-browser-cleanup-r74.md`
- Runtime scope: host-only static and unit checks; no QEMU or hardware access.

The six remaining scanner candidates were classified individually. All were
retained because at least one of the following held: dynamic builder/overlay
consumption, frozen artifact provenance, unknown ownership, documented public
diagnostic entry, or uncommitted changes plus a dynamic import.

## Verification results

| Check | Result |
| --- | --- |
| `python3 -m unittest tools.riscv.tests.test_refactor_reference_scan -q` | pass; 3 tests |
| `refactor_reference_scan.py --verify docs/porting/refactor-deletions.json` | pass; 571 candidates scanned |
| `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests tools/riscv` | pass |
| `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh` | pass |
| JSON parse of deletion manifest and browser-web overlay | pass |
| `git diff --check` | pass |
| `make test_riscv_debian_rootfs_unit` in `asterinas-qemu-gl-runtime` | pass; 329 tests |
| `bash tools/riscv/firefox_fast_check.sh` in `asterinas-qemu-gl-runtime` | pass; 329 tests; `FIREFOX_FAST_CHECK_PASS` |
| `make test_riscv_firefox_daily_use_physical_unit` in `asterinas-qemu-gl-runtime` | pass; 373 tests, 3 skipped |
