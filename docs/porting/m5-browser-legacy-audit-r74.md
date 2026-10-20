# M5/browser-web legacy audit r74

Date: 2026-10-20

This is a host-only second pass over the M5/browser-web inventory. The active
E1–E5 roots remain `firefox_fast_check.sh`, the rootfs unit target,
`desktop_m4_gate.py`, `firefox_drm_scene_gate.py`, the recovery gates, and the
control-chain tests. A file was removable only when the reference scanner,
literal repository search, Makefile search, and the dirty-worktree check all
showed no management-chain dependency.

## Confirmed deletions

| Path | Evidence | Decision |
| --- | --- | --- |
| `tools/riscv/debian/rootfs/firefox_startup_profile.py` | Only the removed diagnostic unit and an obsolete README invocation referenced it; no builder, Makefile, overlay, E1–E5 gate, or control-chain caller | removed |
| `tools/riscv/tests/test_firefox_debug_tool.py` | No Makefile or fast-check entry; it only tested the removed startup sampler and the standalone diagnostic helper | removed |

The README startup-profile block was removed with these files. The standalone
`firefox_debug_tool.py` preflight/manifest helper remains because it has its
own documented use and is independent of the removed sampler.

## Explicitly retained

* `browser_web_*`, `desktop_m5_*`, `desktop_m6_*`, and `desktop_m7_*` remain
  in the builder/legacy adapter graph. Removing them requires a separate
  profile migration and would otherwise break imports or generated rootfs
  manifests.
* `browser_m5_marionette_gate.py` remains an E3 dependency of
  `firefox_drm_scene_gate.py` and its Marionette probe overlay.
* `firefox_compat/build_firefox_esr.sh` and `compat_overlay.py` remain frozen
  artifacts under the approved official-Debian-package decision; they are not
  deleted in this pass.
* `firefox_gdb_probe.sh` remains because the README documents it as a
  diagnostic entry point. `firefox_disabled.service` remains untracked with
  unknown ownership and is not deleted automatically.

No QEMU, Firefox, serial, board, image build, or reboot is part of this pass.
