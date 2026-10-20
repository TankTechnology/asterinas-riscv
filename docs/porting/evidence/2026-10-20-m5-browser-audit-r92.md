# M5/browser audit r92

- Gate: `m5-browser-legacy-audit`
- Result: `passed=true` (audit complete; zero additional safe deletions)
- Scope: M5/browser-web candidates listed in `docs/porting/m5-browser-legacy-audit-r92.md`.
- Scanner: `refactor_reference_scan.py` found 211 whole-tree candidates; manifest verification passed.
- Runtime scope: no QEMU, Firefox, serial, board, image build, reboot, or network operation.

Validation already completed for this code state:

| Check | Result |
| --- | --- |
| `refactor_reference_scan.py --verify docs/porting/refactor-deletions.json` | PASS; 211 candidates scanned |
| `python3 -m unittest tools.riscv.tests.test_debian_dev_overlay tools.riscv.tests.test_debian_browser_web -q` | PASS; 119 tests |
| `make test_riscv_debian_rootfs_unit` in `asterinas-qemu-gl-env-r2` | PASS; 331 tests |
| `firefox_fast_check.sh` in `asterinas-qemu-gl-env-r2` | PASS; 331 tests; `FIREFOX_FAST_CHECK_PASS` |
| `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests` | PASS |
| `bash -n` for changed shell/build scripts | PASS |
| `git diff --check` | PASS |

The host fast-check was also attempted; its only failure was the host's missing
`/usr/local/qemu/bin/qemu-img`, not a test assertion. The persistent container
has that tool and is the authoritative run for this suite.
