# E3 Firefox runtime-profile contract r114

- gate: `e3-firefox-profile-runtime-contract`
- claim: E3 static probe preparation
- status: `pass`
- runtime status: deferred; `/mnt/shared` has 58G free and the 60G pre-gate floor is not met

The short Firefox probe now has an opt-in, validated profile path that can be
placed on `/run` (tmpfs) while the normal scene keeps its stable home profile.
The host gate propagates that path only in probe-only mode. This directly
addresses the r111c observation of a Web Content child in `D (disk sleep)`
after `WebDriver:NewSession` send completion, without changing the Debian
Firefox binary or starting another runtime.

Verification:

- `test_firefox_drm_scene_gate`: 38 tests passed
- `test_megrez_debug_desktop` + `test_debian_browser_web`: 103 tests passed
- Python syntax compilation and `git diff --check`: passed
- `docs/porting/verify.py`: receipt diagnostics passed; overall exit remains 1
  only because E3 runtime and E5 hardware claims are incomplete

No QEMU, Firefox runtime, serial, network, or hardware operation was run.
