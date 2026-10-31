# Case: desktop/browser gate pick (2026-10-31)

## State

- Upstream base: `upstream/main` = `cdd5bc109`.
- Rebase worktree: `feature/ext4-jbd2` / `rebase/upstream-20261029`
  = `31886c061` when this case was measured.
- Source branch: `feature/qemu-network-runtime` = `af48751df`.
- Current rebase line: 48 commits over upstream (23 feat/fix, 4 test,
  20 docs, 1 chore).
- Source branch since 2026-09-01: 880 non-merge commits, 879 authored by the
  downstream developer.

## Trial results

### `0dfa6f615` — two-boot daily-use gate

`git cherry-pick --no-commit 0dfa6f615` applied cleanly to the rebase line
(Makefile auto-merged; `daily_use_gate.py` and its test added). It was still
not runnable: the source module imports six modules absent from the current
tree:

- `tools.riscv.debian.rootfs.desktop_m5_qemu_gate`
- `tools.riscv.debian.rootfs.gate_protocol`
- `tools.riscv.debian.rootfs.gate_runtime`
- `tools.riscv.debian.rootfs.rootfs_gate`
- `tools.riscv.debian.rootfs.rootfs_gate_backend`
- `tools.riscv.debian.rootfs.systemd_m2_gate`

Conclusion: clean textual pick is not a dependency closure.

### `a0d6f2938` — browser-web desktop shell gate

Picking it alone produced eight conflicts, including:

- `tools/riscv/debian/rootfs/README.md`
- `tools/riscv/debian/rootfs/asterinas-terminal.desktop`
- `tools/riscv/debian/rootfs/browser_web_open_firefox.sh` (add/add)
- `tools/riscv/debian/rootfs/build_rootfs.sh`
- `tools/riscv/debian/rootfs/desktop_m5_session.sh` (modify/delete)
- `tools/riscv/debian/rootfs/desktop_online_lxpanel.conf` (add/add)
- `tools/riscv/tests/test_debian_browser_web.py` (modify/delete)
- `tools/riscv/tests/test_debian_rootfs.py` (modify/delete)

The gate also imports the old gate framework. It must be picked as a cluster
after its leaves.

### Closure sizes

| Target | Source closure | Missing on rebase line |
|---|---:|---:|
| `browser_web_desktop_shell_qemu_gate` | 24 | 18 |
| `browser_web_qemu_gate` | 19 | 13 |
| `desktop_m4_gate` | 10 | 7 |
| `desktop_m3_gate` | 9 | 6 |
| `rootfs_gate` | 7 | 4 |

The desktop gate closure includes old M3/M4/network gates, Megrez network
fixture/proxy modules, U-Boot helpers, QMP/PPM helpers, and the gate framework.
It cannot be ported by one `git cherry-pick`.

## Recommended pick order for the desktop target

1. Leaf helpers with no local imports:
   - `tools/riscv/qemu_qmp.py`
   - `tools/riscv/qemu_ppm.py`
2. Gate framework:
   - `gate_protocol.py`
   - `gate_runtime.py`
   - `rootfs_gate.py`
   - `rootfs_gate_backend.py`
   - `systemd_m2_gate.py`
3. Current-compatible browser/desktop helpers after replacing old M4/M5 names
   and dropping Megrez-only dependencies.
4. `browser_web_qemu_gate.py` and the desktop shell gate.
5. Current Makefile/product-gate integration and evidence.

At step 2, adapt against the current `contract.py`, `profiles.py`, and
`signed_sources.py`; do not replace them with the older copies.

## First pick executed

The two leaf helpers were picked into the rebase line in `961b7ceb7`
(`feat(riscv): pick QMP screendump and PPM audit helpers`). Added focused tests:
7 for QMP event/path validation and 4 for PPM audit. The host baseline moved
from 81 tests / 9 modules to **92 tests / 11 modules**, all green. The next
target is the gate framework, not the desktop gate itself.

## Path/contract observations

- The current line has no `browser_web_qemu_gate.py` host gate; it only keeps
  the guest-side `browser_web_marionette_gate.py` and runtime scripts.
- The current line has no `qemu_qmp.py`, `qemu_ppm.py`, or `qemu_uboot_*`
  helpers.
- Old gate config is U-Boot/Stage1 shaped (`GateConfig` includes `u_boot`,
  `dtb`, `stage1_initramfs`), while the rebase line's direct gate is
  `kernel + rootfs` shaped. The adapted gate should reuse the current Stage1
  assets rather than resurrect the old boot flow blindly.
- Keep historical evidence directories; create new ones for new runs.
