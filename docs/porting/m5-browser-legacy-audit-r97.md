# M5/browser-web legacy audit r97

Date: 2026-10-20

This pass separates files that are still part of the published `browser-m5`
or `browser-web` builder graph from genuinely orphaned diagnostics.  A file is
considered a management-chain dependency when it is selected by a profile,
installed by `build_rootfs.sh`/`build_stage1.sh`, imported by an E1--E5 gate or
management adapter, or validated by a maintained unit test.

## Deletion candidates

| Path | Evidence | Decision |
| --- | --- | --- |
| `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` | Only the rootfs README mentioned it; no source, Makefile, profile, overlay, E1--E5 gate, or maintained test invokes it. | **delete** |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | Public `make build_riscv_debian_browser_web_dev_overlay` input; dirty and dynamically consumed by the overlay workflow. | retain |
| `tools/riscv/debian/rootfs/browser_m5.py` and `browser_m5_probe.html` | `build_rootfs.sh` validates and installs the browser-m5 fixture. | retain |
| `tools/riscv/debian/rootfs/browser_m5.webm.base64` | Decoded by the browser-m5 profile during rootfs construction. | retain |
| `tools/riscv/debian/rootfs/browser_m5_{firefox,network_observer,window_observer}.sh` | Installed by the browser-m5 profile and checked by the runtime contract test. | retain |
| `tools/riscv/debian/rootfs/browser_m5_qemu_gate.py` | Imported by the maintained browser-web unit suite; removing it would leave that suite broken. | retain pending test migration |
| `tools/riscv/debian/rootfs/desktop_m5_*.{py,sh}` and `browser_web_*` | Imported or installed by legacy adapters and the public browser-web profile. | retain pending profile retirement |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | Untracked file with unknown ownership and no complete replacement mapping. | retain; unknown |

## Safety check

The deletion above is documentation-only outside the obsolete diagnostic:
removing it cannot alter the rootfs builder, the Firefox scene, or the E1--E5
management chain.  The browser-m5/browser-web profile branch remains intact so
that existing reproducible artifacts and tests are not silently invalidated.

