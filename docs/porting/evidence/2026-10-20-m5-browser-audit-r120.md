# M5/browser-web legacy audit r120

- Gate: `m5-browser-legacy-audit`
- Scope: repository-only audit; no QEMU, Firefox, serial, network, or board action.
- Scanner result: 206 candidates; `refactor-deletions.json` validation passed.

## Deletion list

There are no additional safe code deletions in this round.  The only item
confirmed orphaned by the preceding audit was removed in r113:
`tools/riscv/debian/rootfs/desktop_m5_evidence.sh`.

## Deferred items

| Path | Decision | Reason |
|---|---|---|
| `tools/riscv/debian/rootfs/browser_web_dev_overlay.json` | retain/defer | selected by the documented browser-web development-overlay workflow; it carries active browser services and management assets; the deletion manifest also records existing dirty changes |
| `tools/riscv/tests/test_debian_desktop_m4_x11_gate.py` | retain/defer | historical wrapper has existing dirty changes and documentation references; deleting it would discard unreviewed worktree state |
| `tools/riscv/debian/rootfs/browser_m5_*` and `browser_web_*` | retain | scanner finds build, profile, Stage1, overlay, or management references; they remain part of the supported browser-web workflow |

No deferred item was deleted.  The current M5/browser cleanup receipt is
`docs/porting/receipts/2026-10-20-m5-browser-cleanup-r113.json`.

## Verification

The dependent static and unit suites were rerun after cleanup: browser-web
(94), dev-overlay (24), reference scanner (3), daily-use (62), Firefox daily
use (16), Megrez debug (65), Megrez desktop debug (9), and GMAC gate (33)
passed; Python compileall and `git diff --check` passed.  The full rootfs
collection ran 229 tests with 228 passes and one host-only `qemu-img` missing
error, as recorded in `2026-10-20-storage-cleanup-r119.md`.

