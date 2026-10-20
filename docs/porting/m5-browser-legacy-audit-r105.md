# M5/browser-web legacy audit r105

- gate: `m5-browser-legacy-cleanup`
- scope: host-only reference audit; no QEMU, Firefox, serial, network, or board action

## Deletion list

This round has **no safe deletion**. Every implementation file in the `browser-m5`/`browser-web` family is either actively referenced or is a dynamically selected overlay input. The previous confirmed orphan deletions remain recorded in `refactor-deletions.json` and are not repeated.

| Path/category | Evidence | Decision |
| --- | --- | --- |
| `tools/riscv/debian/rootfs/browser_m5.py`, `browser_m5_probe.html`, `browser_m5.webm.base64`, `browser_m5_{firefox,network_observer,window_observer}.sh` | `build_rootfs.sh` selects and installs these assets for the published `browser-m5` profile; rootfs tests cover the graph. | retain |
| `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py` | Used by `build_rootfs.sh`, `build_stage1.sh`, `firefox_drm_scene_gate.py`, browser daily-use and Marionette paths. | retain |
| `tools/riscv/debian/rootfs/browser_web_*.{py,sh,service,conf,json}` | `browser-web` profile, `build_rootfs.sh`, `build_stage1.sh`, `test_debian_browser_web.py`, and the dynamic overlay select these files. | retain |
| `tools/riscv/debian/rootfs/desktop_m5_*.{py,sh,conf}` | Imported by legacy adapters (`desktop_m6`, debug console, single-packet and Megrez paths) and the browser-web profile. | retain |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | The scanner reports no literal caller, but its profile-named path is supplied dynamically to the overlay workflow and its contents name active M5/browser files. | **unknown; retain** |
| `tools/riscv/KE-M5-report.md` and historical M5 docs | Documentation/traceability only. | retain |

## Active-reference proof

The reference scanner reports active callers for all source and test candidates above. Representative callers are `tools/riscv/debian/rootfs/build_rootfs.sh`, `tools/riscv/debian/rootfs/build_stage1.sh`, `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`, `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`, `tools/riscv/megrez_gmac_gate.py`, `tools/riscv/megrez_firefox_browse.py`, and `tools/riscv/tests/test_debian_browser_web.py`. A file-level deletion would leave an installed path, import, or profile entry dangling.

## Decision

No new file was deleted in r105. The correct next cleanup unit is a deliberate **profile retirement**: remove `browser-m5`/`browser-web` from the public profile API, migrate all listed adapters and rootfs tests, then re-run this audit. Until that migration exists, deleting individual files is unsafe.
