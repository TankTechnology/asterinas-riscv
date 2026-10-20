# M5/browser-web legacy audit r92

Date: 2026-10-20

This is a read-only follow-up after the r75 cleanup. The reference scanner
reported 211 candidates in the whole tools tree. The M5/browser-web scope was
reviewed separately; a candidate is removable only when it has no builder,
Makefile, overlay, E1–E5 gate, control-chain, documented diagnostic, or dirty
worktree dependency.

| Path | Scanner signal | Decision | Reason |
| --- | --- | --- | --- |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | no literal source import | keep | It is the documented browser-web overlay consumed dynamically by the rootfs/stage1 builders and tests. |
| `tools/riscv/debian/rootfs/desktop_m4_firefox_topology_overlay.json` | newly added, no literal import | keep | It is the explicit r90/r91 derivative-image input; deleting it would break the reproducible topology artifacts. |
| `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` | no runtime caller | keep | Frozen cross-build provenance for existing artifacts; the approved package route freezes execution but does not erase provenance. |
| `tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py` | no runtime caller | keep | Same frozen provenance boundary as the builder artifacts. |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | no literal caller | keep | Untracked service with unknown ownership; deletion would be irreversible without an owner decision. |
| `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` | no executable caller | keep | Public documented diagnostic entry point; retire only with an explicit documentation decision. |
| `tools/riscv/tests/test_debian_desktop_m4_x11_gate.py` | no import caller | keep | Tracked file has dirty display-contract changes; deleting it would discard unreviewed work. |
| `tools/riscv/tests/test_debian_m5_network.py` | no E1–E5 import caller | keep | Tracked file has dirty display/network contract changes; it remains part of the current test surface. |

No item in this scope meets all deletion conditions. The two confirmed orphan
deletions remain recorded in r74; no further deletion is performed in r92.
