# M5/browser-web legacy audit r98

Date: 2026-10-20  
Gate: `m5-browser-legacy-cleanup`

## Deletion candidates

| Path | Repository evidence | Decision |
| --- | --- | --- |
| `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py` | Only imported by the browser-web unit test to compare the retired M5 marker set; no builder, profile, overlay, E1–E5 gate, Make target, or management adapter calls it. | **delete** |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | Selected by the public browser-web overlay workflow and consumed dynamically by the rootfs/Stage1 builders. | retain |
| `tools/riscv/debian/rootfs/browser_m5.py`, `browser_m5_probe.html`, `browser_m5.webm.base64` | Browser-m5 profile fixture validation and installation in `build_rootfs.sh`. | retain |
| `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py` | Shared transport imported by Firefox DRM, browser-web, daily-use, and diagnostic paths. | retain |
| `tools/riscv/debian/rootfs/browser_web_*.{py,sh,service,json}` | Browser-web profile inputs installed or validated by the builder/overlay; the profile remains a supported artifact. | retain |
| `tools/riscv/debian/rootfs/desktop_m5_*.{py,sh}` | Desktop/network profile and adapters still reference these files. | retain |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | Untracked file with unknown ownership and no replacement mapping. | retain; unknown |

## Action

Deleted only `browser_m5_qemu_gate.py`.  Removed the unit-test import and its
retired-marker comparison; the browser-web gate still tests its own current
marker contract.  No profile, rootfs, overlay, Stage1, E1–E5, or management
entrypoint was changed.

## Verification scope

The reference scanner found 208 candidates and accepted the deletion manifest.
Focused browser/management tests and static checks pass.  The complete unit
discovery is run separately; unrelated dirty desktop/boot contract failures
are reported as baseline failures rather than attributed to this deletion.
