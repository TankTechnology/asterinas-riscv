# M5/browser-web legacy audit r75

Date: 2026-10-20

This pass audits the remaining browser-web and M5 candidates after r74. The
deletion rule is strict: a path is removable only when it has no builder,
Makefile, overlay, E1–E5 gate, control-chain, or dynamic test dependency, and
the worktree contains no uncommitted change that would be discarded.

## Candidate list and decision

| Path | Why it appeared as a candidate | Decision | Reason |
| --- | --- | --- | --- |
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | The scanner sees no literal code import | keep | It is the documented `browser-web` overlay input; README/build tooling consume it indirectly. It also has uncommitted additions for the current display path. |
| `tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh` | Frozen cross-builder helper has no E1–E5 runtime caller | keep | The approved package decision freezes, but does not erase, the reproducibility anchor for existing builder artifacts. |
| `tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py` | Frozen compatibility overlay helper has no E1–E5 runtime caller | keep | Same provenance boundary as the frozen cross-builder artifacts; deletion would remove the documented reconstruction path. |
| `tools/riscv/debian/rootfs/firefox_disabled.service` | Untracked service with only historical/documentation hits | keep | Ownership is unknown; it is not safe to delete an untracked file automatically. |
| `tools/riscv/debian/rootfs/firefox_gdb_probe.sh` | No executable caller found by the scanner | keep | The README documents it as a diagnostic entry point; retain until that public entry is retired explicitly. |
| `tools/riscv/tests/test_debian_m5_network.py` | No literal import from the E1–E5 roots | keep | The file has uncommitted display-contract additions. Deleting it would discard dirty work, so it is retained until that diff is explicitly reconciled. |

No candidate satisfied every deletion condition, so this pass intentionally
deletes nothing. The two confirmed orphan deletions remain recorded in
`m5-browser-legacy-audit-r74.md` and the deletion manifest.

## Verification scope

The checks below cover the reference scanner, Python syntax, shell syntax,
manifest JSON, and the persistent rootfs/browser unit suites. No QEMU, Firefox
runtime, serial, board, image build, or reboot is part of this audit.
