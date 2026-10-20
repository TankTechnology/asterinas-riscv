# M5/browser-web cleanup r105

- gate: `m5-browser-legacy-cleanup`
- claim: E3 supplemental cleanup receipt; it does not claim Firefox runtime success
- status: `pass` (audit completed; no unsafe deletion performed)
- manifest SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
- audit SHA-256: `73c95a8b1c37825440269caaa4fa44f8f12165a4c807a5e8d04f63cf3935163c`

## Result

The candidate list contains no file that is both (1) a source/test input in the M5/browser-web family and (2) proven free of builder, profile, overlay, test, or management-chain dependencies. `browser_web_dev_overlay.json` has no literal filename caller, but is a dynamic profile overlay and contains active file entries, so it remains an explicit unknown rather than a deletion. All other candidates have active references.

The prior confirmed deletions remain in `docs/porting/refactor-deletions.json`; this round made no source deletion and did not touch QEMU, Firefox, serial, network, or hardware.

## Verification commands

The following commands are the required post-audit checks and are recorded in the accompanying receipt:

- `python3 tools/riscv/refactor_reference_scan.py --repository . --root tools/riscv/debian/rootfs --tests tools/riscv/tests --makefile Makefile --output docs/porting/refactor-reference-report.md --verify docs/porting/refactor-deletions.json`
- `python3 -m unittest tools.riscv.tests.test_refactor_reference_scan tools.riscv.tests.test_browser_daily_use_gate tools.riscv.tests.test_debian_browser_web tools.riscv.tests.test_debian_rootfs -q`
- `bash tools/riscv/firefox_fast_check.sh` in the persistent development container
- `git diff --check`

## Results

- Reference scanner: **pass**, 207 candidates scanned; manifest verification exit 0.
- Reference-scanner unit tests: **pass**, 3 tests.
- Persistent-container `firefox_fast_check.sh`: **pass**, 337 tests; emitted `FIREFOX_FAST_CHECK_PASS`. This includes Python compilation, shell syntax checks, and `git diff --check`.
- No QEMU process, Firefox scene, serial write, network traffic, or hardware operation was started by this cleanup.

The host Python environment lacks `/usr/local/qemu/bin/qemu-img`; the authoritative fast check was therefore run in the persistent project container required by AGENTS.md.
