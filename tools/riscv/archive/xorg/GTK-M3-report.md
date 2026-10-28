# GTK M3 — pcmanfm + xterm on a multi-window Asterinas RISC-V desktop

**Status:** milestone reached (file manager + terminal + panel + WM borders, four windows tiled on screen)
**Date:** 2026-08-14
**Scope:** integrate the already-cross-compiled `pcmanfm` (file manager) and `xterm`
(terminal) into the desktop session, re-examine the M2 "~20 MB initramfs limit", and
make the window manager show *multiple* windows at once instead of one full-screen app.

---

## 1. Milestone summary

The desktop session now runs `matchbox-window-manager` with four GTK2/X11 client
windows — `pcmanfm` (file manager, browsing `/`), `xterm` (terminal), and two
`gtk-hello` demo apps — plus the `xpanel` top bar, all rendering on Xorg over the
Asterinas RISC-V framebuffer. The M3 screenshot
(`target/qemu-uboot/riscv-gtk2-m3-desktop.ppm`, 1280×1024) pixel histogram:

| color | pixels | meaning |
|---|---|---|
| `#DCDAD5` | 48% | two `gtk-hello` client windows |
| `#FFFFFF` | 41% | `xterm` + `pcmanfm` file-list background |
| `#202028` | 3.8% | `xpanel` bar |
| `#496179` | 3% | four matchbox-wm frame **titlebars** |
| `#000000` | ~1.5% | label / file-name / terminal text |
| `#9E9A91` / `#D3D7B8` … | <1% | pcmanfm toolbar + list widget greys |

Three things had to change on the way:

1. **The M2 "~20 MB initramfs limit" was a misdiagnosis.** The larger initramfs is not
   a hard kernel hang — it boots, just slowly. The `qemu_desktop_boot.py` 120 s
   userspace deadline turned a slow boot into a false "hang" (§2).
2. **matchbox-wm maximizes every app window.** By default it is a *phone/tablet* WM:
   every app client is made full-screen and stacked, so only the focused one is ever
   visible. A small source patch tiles the app clients in a 2-column grid (§3).
3. **`xpanel` was being managed as an app window.** Without a window-type hint,
   matchbox treated the panel as a normal app and gave it a full tile. Setting
   `_NET_WM_WINDOW_TYPE_DOCK` makes matchbox keep it as a full-width top bar (§4).

---

## 2. The M2 "~20 MB initramfs limit" is a slow boot, not a hang

### 2.1 Re-examining the symptom

M2 reported that a gzipped initramfs above ~20 MB stalls at
`Spawn the first kernel thread`, before `unpacking initramfs`, and concluded the stall
was "before the initramfs is even decompressed". This milestone instrumented
`kernel/src/fs/rootfs.rs` and re-booted a 27.7 MB initramfs (pcmanfm + xterm bundled).
The instrumentation (`initramfs buffer: 27718495 bytes` → `gzip magic detected,
decompressing ...` → `decompressed 62859264 bytes` → `unpacking initramfs` →
`rootfs is ready`) proves the decompression **completes**; it is merely slow.

A host-side check confirmed the same `zune-inflate 0.2.54` decompresses the 27.7 MB
file in ~190 ms natively. On the emulated RISC-V guest the gzip decode plus the CPIO
unpack of ~63 MB takes on the order of a minute, and Xorg then needs its usual
~170 s to finish initialising. The total time to userspace is ~190 s, which is past the
120 s deadline in `qemu_desktop_boot.py` — so the runner prints the (misleading)
`[warn] userspace marker not reached` and the earlier report read that as a genuine
stall.

### 2.2 Resolution

No kernel fix is needed. The initramfs is trimmed to ~27.7 MB (still well under the
machine's 2 GiB), and the runner's userspace deadline is raised from 120 s to 300 s
(`tools/riscv/qemu_desktop_boot.py`) so a slow boot is not mistaken for a hang.
The M2 kernel-side "gap" (§4 of that report) is therefore **closed by correction**,
not by a code change: the earlier 20 MB threshold was an artefact of the timeout.

---

## 3. matchbox-wm — tiling app clients instead of maximizing

matchbox-window-manager 1.2.2 is a small-screen (PDA/tablet) WM: `main_client_configure`
sizes every `MBCLIENT_TYPE_APP` client to `dpy_width × (dpy_height − title)` and stacks
them, so only the focused window is visible. With four app clients that meant the
screenshot showed one white `xterm` and nothing else.

A downstream source patch (in `target/riscv-cross/src/matchbox-window-manager-1.2.2/`,
outside this repo's git, like the xserver patches) changes two things:

1. **`main_client_configure`** — instead of full-screen, each app client is placed in a
   `n_cols = 2` grid. The index is taken from the age-ordered `client_age_list` so the
   layout is stable across focus changes:
   ```c
   int n_apps = 0, my_idx = 0;
   list_enumerate(w->client_age_list, item) {
       Client *p = (Client *) item->data;
       if (p && p->type == MBCLIENT_TYPE_APP && !(p->flags & CLIENT_IS_MINIMIZED)) {
           if (p == c) my_idx = n_apps; n_apps++;
       }
   }
   n_cols = (n_apps > 1) ? 2 : 1;
   n_rows = (n_apps + n_cols - 1) / n_cols;
   frame_w = (dpy_width  - east - west) / n_cols;
   frame_h = (dpy_height - north - h - offset_south) / n_rows;
   c->x = west + (my_idx % n_cols) * frame_w + offset_west;
   c->y = north + (my_idx / n_cols) * frame_h + frm_size;
   c->width  = frame_w - offset_west - offset_east;
   c->height = frame_h - frm_size - offset_south;
   ```

2. **`main_client_relayout`** — stock matchbox only configures the newly-mapped client,
   which leaves existing windows at their stale full-screen geometry. A new
   `main_client_relayout(w)` re-runs `configure` + `move_resize` +
   `client_deliver_config` on every app client, and is called from
   `wm_make_new_client` after the new window is set up.

The binary is rebuilt in place (`make` with `-fcommon`, then strip + copy to
`target/riscv-cross/usr/bin/matchbox-window-manager`), same 2.4 MB as before.

---

## 4. xpanel as an EWMH dock

`xpanel` (a hand-written pure-X11 panel) creates a full-width window at `(0,0)` but set
no window-type hint, so matchbox managed it as a normal app client and gave it a tile
— its `#202028` background then filled a whole quarter of the screen. The fix sets the
EWMH dock type before mapping (`tools/riscv/xorg/xpanel.c`):

```c
Atom wm_type = XInternAtom(dpy, "_NET_WM_WINDOW_TYPE", False);
Atom wm_dock = XInternAtom(dpy, "_NET_WM_WINDOW_TYPE_DOCK", False);
XChangeProperty(dpy, panel, wm_type, XA_ATOM, 32, PropModeReplace,
                (unsigned char *)&wm_dock, 1);
```

matchbox classifies it as a `MBCLIENT_TYPE_PANEL` / `CLIENT_DOCK_NORTH`, keeps it at its
requested `PANEL_H` height as a full-width top bar, and `wm_get_offsets_size(…, NORTH, …)`
now returns the panel height so the tiled app windows start below it. `xpanel.c` gained
`#include <X11/Xatom.h>` for `XA_ATOM` and was recompiled statically against libX11.

---

## 5. pcmanfm and xterm in the session

`init.c` now spawns both through the respawn loop (they exit if X is not up yet, like
matchbox-wm/xterm):

```c
char *pcmanfm_argv[] = { "/usr/bin/pcmanfm", "/", NULL };
spawn_retrying_argv(pcmanfm_argv);   /* file manager, browse / */
spawn_retrying("/usr/bin/xterm");    /* terminal */
```

`build_xorg_initramfs.sh` bundles `pcmanfm` + `xterm` and the pcmanfm/libfm runtime data
(GTK builder `.ui` files, `folder.png`/`unknown.png`, `.desktop` entries), and creates
`/root` (pcmanfm's HOME).

Runtime notes (all non-fatal):

- **pcmanfm** lists `/` and renders its toolbar + file list. It logs
  `~/Templates doesn't exist`, `modules directory is not accessible` (the gio plugin
  modules were dropped at static-build time, M2 §3.2), `Could not find the icon
  'media-eject'. The 'hicolor' theme` (no icon theme bundled — the file list shows
  names without icons), and `Error creating IO channel for /proc/mounts` (no `/proc`
  mount). None of these stop it browsing files.
- **xterm** runs its shell (the PTY/controlling-terminal fix from the previous session
  is still in place). It logs `Failed to open input method` because no XIM server is
  present; this is non-fatal — xterm continues without an input method. The single
  `xorg: client exited, retrying` in the log is matchbox-wm's own first
  display-not-ready attempt, not xterm.

---

## 6. Session and artifacts

Default session (`tools/riscv/xorg/init.c`): Xorg → `matchbox-window-manager` →
`xpanel` → `gtk-hello` ×2 → `pcmanfm /` → `xterm`. The four app windows are tiled
2×2 under the dock panel.

| file | what it is |
|---|---|
| `tools/riscv/xorg/GTK-M3-report.md` | this report |
| `tools/riscv/xorg/init.c` | session: matchbox-wm + xpanel + 2× gtk-hello + pcmanfm + xterm |
| `tools/riscv/xorg/xpanel.c` | panel; now sets `_NET_WM_WINDOW_TYPE_DOCK` |
| `tools/riscv/xorg/build_xorg_initramfs.sh` | initramfs packaging: + pcmanfm/xterm + data + `/root` |
| `tools/riscv/qemu_desktop_boot.py` | runner: userspace deadline 120 s → 300 s |
| `target/qemu-uboot/riscv-gtk2-m3-desktop.ppm` | the milestone screenshot |
| matchbox-wm `src/main_client.c` / `src/wm.c` | the app-client tiling patch (in `target/riscv-cross/src/`, not this repo's git) |

### Known limitations (left for future work)

- The tiling is a bring-up patch: a fixed 2-column grid, no interactive resize of the
  split, and windows added later are re-tiled in age order. A real tiling policy is out
  of scope.
- pcmanfm has no icon theme (blank file icons) and no `/proc` mount (no mount/device
  views). Both are initramfs-content gaps, not kernel gaps.
- xterm's input-method warning is cosmetic; there is no XIM server in the session.
