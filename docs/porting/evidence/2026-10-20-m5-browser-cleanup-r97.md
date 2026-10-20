# M5/browser-web cleanup r97

Date: 2026-10-20
Gate: `m5-browser-legacy-cleanup`

## Scope

The audit in [`docs/porting/m5-browser-legacy-audit-r97.md`](../m5-browser-legacy-audit-r97.md)
classified the remaining M5/browser-web files by actual builder, profile,
overlay, test, and E1--E5 management-chain references.  The browser-m5 and
browser-web profile branches remain intact because they are still selected by
the rootfs builder and consumed by legacy adapters/tests.  Their fixture,
Marionette, overlay, and service files were not deleted.

## Deletion

* `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` — removed.  Its only
  repository reference was the README command that was removed in the same
  change; no code, Makefile, profile, overlay, E1--E5 gate, or management
  adapter invokes it.  The current bounded Firefox probe remains separate.

## Verification

* Reference scanner: `scanned 209 candidates`, `--verify` exit 0.
* JSON manifest validation, Python `compileall`, shell `bash -n`, and
  `git diff --check`: pass.
* Focused browser/management suite: 196 tests, all pass.
* Firefox fast check in the persistent QEMU environment: 334 tests, all pass,
  `FIREFOX_FAST_CHECK_PASS`.
* The first host-only fast-check attempt reported the pre-existing missing
  `/usr/local/qemu/bin/qemu-img`; rerunning in `asterinas-qemu-gl-env-r2`, where
  that tool is present, passed without launching a guest.

The complete discovery run
`python3 -m unittest discover -s tools/riscv/tests -p 'test*.py' -q` was also
attempted. It ran 1,863 tests but ended with 24 failures and 8 errors in
pre-existing dirty desktop/boot-contract fixtures (for example,
`desktop-native-qemu-contract-drift`, the changed `--volatile-home` bootarg,
and boot-stability recovery expectations). None of those failures references
the deleted diagnostic or the browser-m5/browser-web dependency graph; they are
recorded here so this cleanup is not reported as a full-suite pass. The focused
browser/management suite and fast check above remain green.

No QEMU guest, hardware, serial, or network experiment was run by this cleanup.

