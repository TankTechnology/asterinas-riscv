# M5/browser-web cleanup r102 evidence

- gate: `m5-browser-legacy-cleanup`
- status: pass
- deletion candidate: `tools/riscv/tests/test_debian_browser_m5_runtime_gate.py`
- replacement: local `_Socket`/`_frame` fixtures in `tools/riscv/tests/test_browser_daily_use_gate.py`
- retained management-chain inputs: browser-m5 fixture, browser-web overlay/service/runtime, desktop-m5 through m8 adapters, shared Marionette transport
- no guest, serial, hardware, network, or Firefox runtime experiment

## Verification

- pre-change focused suite: 98 tests, pass
- post-change browser/rootfs/overlay suite: 181 tests, pass
- reference scanner and deletion manifest: pass (`207` candidates; no active reference to the deleted module)
- `test_refactor_reference_scan` + `test_megrez_firefox_daily_use`: 19 tests, pass
- persistent-container `tools/riscv/firefox_fast_check.sh`: 337 tests, pass; `FIREFOX_FAST_CHECK_PASS`
- compileall, Python syntax, shell syntax, and `git diff --check`: pass
- full `tools/riscv/tests` discovery: 1,830 tests, 24 failures, 8 errors, 5 skipped. The failures are pre-existing dirty desktop/boot diagnostic baselines; no failure or error mentions the deleted M5 runtime module, and the focused cleanup suites remain green.
- host-only fast check outside the project container: not used as the verdict because `/usr/local/qemu/bin/qemu-img` is unavailable there

## Hashes

- audit: `e344bfb7c45c6ea721caff38b95869a0f490652c16ab100f024be09db0fcfeac`
- reference report: `311efd04ad3c04074e2de62df0c6b3c91179091cfd00685d424aed92b867909f`
- deletion manifest: `7adfbf2eaa39a777ba9cfdd200e8a82b85f07a75313f25b743f6b659cc5b8dfb`
- daily-use test: `1309e3377811456fa3b6aa339304abef7bb5a5de3663eed1f766968b511ab908`
- daily-use test list: `0d1a5d66e728e01b2bf934f9ce715315cb0d5ecf9a8d82587e5b845627e776dc`
