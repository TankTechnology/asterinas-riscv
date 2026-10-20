# M5/browser-web cleanup r98 evidence

- gate: `m5-browser-legacy-cleanup`
- status: pass
- candidate scan: 208
- deletion: `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py`
- test-only dependency removed: `tools/riscv/tests/test_debian_browser_web.py` retired marker comparison
- retained active inputs: browser-m5 fixture, browser-web overlay/service/runtime, desktop-m5 network helpers, shared Marionette transport
- manifest verification: `refactor_reference_scan.py --verify` exit 0
- no QEMU guest, serial, hardware, or network experiment was used for this cleanup

## Hashes

- audit: `c07c82a7b7d8069f1ac7638982dc50d37daa676b9a027b8fbab7f609c06d28cb`
- reference report: `2f65c4a6eeba8f5f0b04f84608b37f1032ec5faffb4a61b64925ef3a42f20026`
- deletion manifest: `ce1d8d0c202d1c9cbf6a2bf82a864f0fe40c7c04a41739ec9f344472aa16ed7b`

## Verification results

- Focused browser/management suite: 196 tests, all pass.
- `firefox_fast_check.sh` in the persistent QEMU container: 335 tests, all pass (`FIREFOX_FAST_CHECK_PASS`).
- `compileall`, shell syntax checks, `git diff --check`, and reference-manifest verification: pass.
- Complete discovery: 1,864 tests, 24 failures and 8 errors. These are existing dirty desktop/boot-contract failures (for example `desktop-native-qemu-contract-drift`, frozen QEMU `-cpu` contract, and boot-stability recovery expectations); none references the deleted wrapper.
- No guest, serial, hardware, or network experiment was run for this cleanup.
