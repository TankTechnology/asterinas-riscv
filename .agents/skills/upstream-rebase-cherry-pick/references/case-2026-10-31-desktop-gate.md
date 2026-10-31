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

## Second pick executed

The gate framework was picked in `6b62ca317`
(`feat(riscv): pick the Debian gate framework`): `gate_protocol.py`,
`gate_runtime.py`, `rootfs_gate.py`, `rootfs_gate_backend.py`, and
`systemd_m2_gate.py`. It imported cleanly against the current
`contract.py`/`profiles.py`/`signed_sources.py`; added 9 focused host tests for
bootargs, QEMU argv, argument defaults, and pinned-output locking. Host
baseline is now **101 tests / 12 modules**. The desktop closure dropped from
18 missing modules to 11; `browser_web_qemu_gate` dropped from 13 to 8.

## Third pick executed

The desktop application gates were picked in `9d597171c`
(`feat(riscv): pick the Debian desktop application gates`): `desktop_m3_gate.py`
and `desktop_m4_gate.py`. They import only the picked framework and the current
contract module. Added 5 focused tests for graphical QEMU argv, ordered
milestones, framebuffer evidence, out-of-order transcripts, fatal markers, and
empty release values. Host baseline is now **106 tests / 13 modules**.
Remaining desktop closure is 9 modules; remaining browser QEMU closure is 6.

## Direct-boot consolidation turn

A direct QEMU boot with `-kernel Image -initrd <full Stage1 cpio> -dtb
fb-bochs.dtb -device bochs-display` reached systemd, Xorg, window manager,
desktop session, and `panel-start` using the current `browser-web` root. This
makes the old generic U-Boot gate framework unnecessary for the QEMU desktop
gate.

Consolidation target:

- keep one `tools/riscv/debian/desktop_gate.py` for direct boot + marker checks
  + QMP screenshot;
- keep `qemu_qmp.py` and `qemu_ppm.py`;
- delete `gate_protocol.py`, `gate_runtime.py`, `rootfs_gate.py`,
  `rootfs_gate_backend.py`, `systemd_m2_gate.py`, `desktop_m3_gate.py`, and
  `desktop_m4_gate.py` after the lean gate passes;
- delete their wrapper tests and keep one `test_desktop_gate.py`.

The old files remain in git history and in the rejected source branch.

## Consolidated result

The direct-boot marker gate passed with all six `BROWSER_WEB_DESKTOP_STAGE`
markers (`display-provider-ready` through `panel-start`). The final production
surface is just `desktop_gate.py`; the QMP/PPM helpers, generic gate framework,
and M3/M4 wrappers were removed. Host tests dropped from 106/13 modules to
**86/10 modules**, and the desktop gate has its own five contract tests.

QMP screendump was not usable as pixel evidence here: even with
`-vga none`, an explicit `bochs-display,id=desktop-display,xres=1280,yres=1024`,
and QMP `device=desktop-display`, it returned a 640x480 VGA image while the
guest used the 1280x1024 simple framebuffer. Treat a screendump as valid only
after checking its PPM dimensions and matching them to the guest display; do
not keep a helper solely because it exists.

## P0: real Firefox and framebuffer evidence (2026-10-31)

The consolidated marker gate was still not display evidence.  A direct boot
with the old `fb-bochs.dtb` reached every `BROWSER_WEB_DESKTOP_STAGE` marker,
but the guest framebuffer at `0x40000000` was an unassigned bochs PCIe BAR:

- QEMU `info pci` showed `BAR0 ... at 0x00000000`;
- `pmemsave 0x40000000` returned all `0xffffffff`;
- the earlier QMP screendump was 640x480, not the guest's 1280x1024 image.

The direct-boot path has no firmware to allocate PCI BARs, so the DTB address
was a hole.  The Xorg/desktop markers only proved process starts, not pixels.

The replacement keeps direct boot and U-Boot out of the gate:

1. `make_fb_ram_dtb.sh` rewrites the QEMU bochs DTB: shrink `memory@80000000`
   by 256 MiB and put the `simple-framebuffer` reg at `0xf0000000` with a
   16 MiB region.  The kernel reserves the region; QEMU still has 2 GiB RAM.
2. The `browser-web` rootfs installs `desktop_frame_evidence.sh` as the root
   `asterinas-desktop-frame-evidence.service`.  It waits for Xorg, settles the
   desktop, finds a visible Firefox window with `xdotool` (fallback
   `xwininfo`), and emits `BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=ready` and
   `BROWSER_WEB_DESKTOP_FRAME_READY source=host-pmemsave`.
3. `desktop_gate.py` opens a private QEMU monitor, dumps the guest framebuffer
   with HMP `pmemsave` (quoted absolute path; an unquoted path is parsed as a
   division expression), validates non-black pixels and colour count, saves
   desktop/final raw+PPM evidence, and requires the Firefox window to change
   the frame.

Observed timing under TCG: the first wallpaper paint can be ~40 s after
`panel-start`; the guest settle is 60 s and the host capture retries while
draining the serial pipe.  The old 15 s settle produced a false "blank
framebuffer" failure.  Unprivileged browser-wrapper output was not reliable on
the serial console, so the observer runs as root and writes its own markers to
`/dev/console`.

Two follow-up failures were contract bugs, not display bugs:

- `browser-web-firefox` exits 64 when `ASTERINAS_WEB_NETWORK_MODE` is empty,
  and its unit has `RestartPreventExitStatus=64`, so basic-only boot left
  `asterinas-browser-web.service` failed.  The gate must also pass
  `systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct`.
- `find_firefox_window` printed `id title` without a trailing newline; `read`
  from the process substitution returned 1 at EOF even after finding a visible
  window.  The observer therefore emitted `FIREFOX_WINDOW=timeout` right after
  the window appeared.  Always terminate helper output with `\n`.

The observer heartbeat is deliberately diagnostic: `service=`, `processes=`,
and `wrapper_pid=` distinguish a failed browser unit, a running Firefox with
no mapped window, and a window that `xdotool` missed.  The first two failures
were fixed from those fields without another full boot.

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
