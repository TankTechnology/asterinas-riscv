# Legacy tooling

Historical drivers and demos that are kept for reproducibility but are no
longer part of the current workflow. They were moved here on 2026-11-09 so the
top-level `tools/riscv/` listing only shows the active tracks.

| Directory | What it is | Current replacement |
| --- | --- | --- |
| `xfce/` | XFCE-M2/M3 cross-build and boot chain (`boot_xfce_desktop.py`, initramfs packer, unit files) | The Debian desktop session running openbox + lxpanel + pcmanfm (`tools/riscv/debian/rootfs/desktop_m5_session.sh`) |
| `lvgl/` | LVGL framebuffer demo | None; the Debian desktop is the supported GUI |
| `compat/` | Developer-workflow acceptance checks (ssh, pty, scp, gcc, tar.xz…) for a guest | Still usable by hand against a running board or QEMU guest |
| `game/` | `snake.c` framebuffer demo | None |

Rules for this tree:

- nothing here is referenced by the Makefile, the host test suite, or the
  current gates;
- paths between these scripts and the rest of the repository were adjusted
  when the directories moved (one extra `..` for shell prologues and
  `parents[4]` for Python root resolution);
- the reports and commands inside `xfce/` are dated (XFCE-M2/M3) and are not
  rewritten when a path moves, matching the policy in
  [`../LAYOUT.md`](../LAYOUT.md#historical-documents);
- the pre-move tree is also reachable at
  `refs/archive/local-2026-11-09-tooling-cleanup`.

Current entry points remain in [`../LAYOUT.md`](../LAYOUT.md) and
[`../../README.md`](../../README.md).
