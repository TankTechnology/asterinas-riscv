# M5/browser-web legacy audit r113

- gate: `m5-browser-legacy-cleanup`
- scope: host-only reference audit; no QEMU, Firefox, serial, network, or board action

## Deletion list (before mutation)

| Path | Reference result | Management-chain check | Decision |
| --- | --- | --- | --- |
| `tools/riscv/debian/rootfs/desktop_m5_evidence.sh` | One reference, from the legacy `test_m5_profile_keeps_formal_markers_without_debug_console_flood` unit test only | Not selected by `build_rootfs.sh`, `build_stage1.sh`, any profile, overlay, systemd unit, Make target, E1–E5 gate, or Megrez management adapter. The active browser-web overlay installs `browser_web_evidence.sh` at the same destination instead. | **delete** |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | No literal code caller; selected by the documented browser-web development-overlay workflow | Its profile name is consumed dynamically by the overlay builder and its entries install active browser-web services and management assets. | retain |
| `tools/riscv/debian/rootfs/browser_m5_*`, `browser_web_*`, `desktop_m5_*`, `desktop_m6_*`, `desktop_m7_*`, `desktop_m8_*` | Active callers in profile/build/Stage1/management paths or dynamic overlay inputs | Removing any would leave an installed path, profile entry, or management adapter dangling. | retain |

The deleted script is an offline M5 evidence implementation that is no longer
installed. Its only test consumer is removed with it; the current browser-web
contract test continues to cover the installed `browser_web_evidence.sh`.

## Verification plan

1. Re-run the reference scanner and manifest validator.
2. Run the browser-web unit suite and the refactor scanner unit suite.
3. Run Python syntax compilation and `git diff --check`.

No runtime gate or hardware operation is part of this cleanup.
