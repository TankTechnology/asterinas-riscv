# M5/browser-web legacy audit r93

Date: 2026-10-20

This pass rechecks the remaining M5/browser-web candidates after r92.  A
candidate is removable only when it has no E1-E5 management-chain reference,
builder/profile selection, overlay input, dynamic test dependency, documented
diagnostic entry point, or uncommitted worktree change.

## Candidate deletion list

| Path | Scanner signal | Decision | Evidence |
| --- | --- | --- | --- |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | zero literal callers; ten documentation hits | retain | `build_rootfs.sh` selects it through the `browser-web` profile; `test_debian_dev_overlay.py` validates its schema and runtime digest; the file is dirty. |
| `tools/riscv/debian/rootfs/desktop_m4_firefox_topology_overlay.json` | zero literal callers | retain | It is the reproducible r90-r93 topology input; it is untracked and deleting it would remove the current derivative-image source. |
| `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` | zero runtime callers | retain/freeze | The approved official-Debian package route freezes execution but keeps provenance for existing artifacts; it is untracked. |
| `tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py` | zero runtime callers | retain/freeze | Same frozen provenance boundary as the builder; it is untracked. |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | zero literal callers | retain | Untracked service with unknown ownership; no deletion authority or replacement mapping exists. |
| `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` | zero executable callers | retain | The rootfs README documents it as a diagnostic entry point. |
| `tools/riscv/tests/test_debian_desktop_m4_x11_gate.py` | zero literal callers | retain | The tracked file has dirty display-contract changes and covers the current E2 X11 path. |
| `tools/riscv/tests/test_debian_m5_network.py` | zero E1-E5 imports; 6/143 targeted tests fail | delete | It is the retired M5 network/browser composite suite. Its 27-line dirty additions only duplicate the E2 display contract, while the remaining assertions target removed M5 Make/QEMU/profile behavior. |
| `tools/riscv/tests/test_desktop_drm_libdrm_probe.py` | zero literal callers | retain | Untracked E2 probe test; the corresponding probe is installed by `build_rootfs.sh`. |
| `tools/riscv/tests/test_desktop_drm_x11_screenshot.py` | zero literal callers | retain | Untracked E2 screenshot test; it validates the current DRM/X11 handoff. |
| `tools/riscv/tests/test_drm_scanout_fault_gate.py` | zero literal callers | retain | Untracked E4 representative fault test; keep as the selected recovery test. |

The previously confirmed orphan deletions remain recorded in r74 and r92.
This pass removes only `test_debian_m5_network.py`; it is not imported by the
E1-E5 fast-check and its targeted run fails against the current contract.  The
remaining M5/browser-web implementation branch still requires a separate
profile and Megrez-adapter migration, so it is retained rather than leaving
dynamic references dangling.

## Verification basis

* `tools/riscv/refactor_reference_scan.py` scanned 211 repository candidates;
  manifest verification passed.
* Literal search covered `Makefile`, both rootfs builders, profiles, overlays,
  E1-E5 roots, and Megrez adapters.
* The worktree was inspected before deletion; dirty files and untracked
  artifacts were not removed.

## Result

`deleted_paths=["tools/riscv/tests/test_debian_m5_network.py"]`.  The legacy
profile implementation is not migrated by this bounded deletion; only its
stale composite test was removed.
