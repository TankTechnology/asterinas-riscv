# M5/browser-web legacy audit r102

Date: 2026-10-20  
Gate: `m5-browser-legacy-cleanup`

## Deletion candidates

| Path | Evidence | Decision |
| --- | --- | --- |
| `tools/riscv/tests/test_debian_browser_m5_runtime_gate.py` | Its only non-document reference was a socket/frame fixture imported by one `browser_daily_use_gate` test. The fixture is now local to that test; the module is not imported by production code, Make targets, profiles, overlays, E1–E5 gates, or management adapters. | **delete** |
| `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py` | Installed by `build_rootfs.sh` and `build_stage1.sh`, and imported by the Firefox DRM scene, browser-web, daily-use, and diagnostic paths. | retain |
| `tools/riscv/debian/rootfs/browser_m5.py`, `browser_m5_probe.html`, `browser_m5.webm.base64`, `browser_m5_{firefox,network_observer,window_observer}.sh` | Selected and installed by the published `browser-m5` profile. | retain |
| `tools/riscv/debian/rootfs/browser_web_*.{py,sh,service,json,conf}` | Selected by the `browser-web` profile and its dynamic overlay; the current browser-web unit test validates the installed graph. | retain |
| `tools/riscv/debian/rootfs/desktop_m5_*.{py,sh,conf}`, `desktop_m6_*`, `desktop_m7_*`, `desktop_m8_*` | Still installed or imported by legacy desktop adapters and the profile builder. Removing one file would leave active callers or generated units dangling. | retain; requires a separate profile retirement |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | Untracked file with no ownership or replacement mapping. | retain; unknown |

## Action

The single test-only module was deleted after moving its two private transport
fixtures (`_Socket` and `_frame`) into `test_browser_daily_use_gate.py`. No
rootfs input, Stage1 artifact, Firefox binary, profile, overlay, E1–E5 gate, or
management-chain entrypoint was removed.

## Verification boundary

The pre-change focused browser suite passed (98 tests). The post-change suite
must cover the same current daily-use behavior and the repository reference
scanner must accept the updated deletion manifest. No QEMU, guest, serial,
network, or hardware operation is part of this cleanup.
