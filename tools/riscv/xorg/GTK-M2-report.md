# GTK M2 — a real window manager + file manager on the Asterinas RISC-V desktop

**Status:** milestone reached (WM + panel + multiple GTK2 windows) + file-manager evaluation
**Date:** 2026-08-14
**Scope:** make `matchbox-window-manager` (a *reparenting* WM) actually render client
content under the shadow fbdev driver, evaluate/build the GTK2 `pcmanfm` file manager,
and assemble the session into one screenshot.

---

## 1. Milestone summary

The desktop session now runs a real reparenting window manager
(`matchbox-window-manager 1.2.2`) with a panel (`xpanel`) and two GTK2 client windows
(`gtk-hello`, spawned twice), all rendering on Xorg 21.1.15 over the Asterinas RISC-V
framebuffer. M1 only proved a single GTK2 window renders; M2 proves the window-management
path — frame decorations, reparented client content, window move/switch — under a real WM.

Two things had to be fixed on the way:

1. **The xserver shadow driver broke reparented client content.** matchbox-wm reparents
   each client into an `override_redirect` frame; under the shadow framebuffer the
   reparented client's clip region came out empty, so the client never painted and never
   got its Expose. A defensive fallback in `miComputeClips` fixes it (§2).
2. **The initramfs early-memory limit.** Every GTK2 binary is ~14-15 MB static; bundling
   more than ~20 MB (gzipped) of initramfs makes the kernel stall at
   `Spawn the first kernel thread` (§4).

---

## 2. matchbox-wm under the shadow driver — root cause + fix

### 2.1 Symptom and prior conclusion

M1 established that `matchbox-window-manager` runs but draws only its frame titlebar
(`#496179`); the reparented client window content never reaches the framebuffer, while
`xwm` (no reparent, border only) renders everything. The M1 report switched the session
WM to `xwm` and left the reparenting issue as a future xserver investigation.

### 2.2 Investigation

The shadow framebuffer architecture is correct and is *not* the blit: `fbScreenInit`
passes `fPtr->shadow` as the screen pixmap (`xf86-video-fbdev/src/fbdev.c:848`), every
window draws into that same pixmap, `shadowAdd` registers damage on it, and the local
`shadowRedisplay` patch already does a full-screen blit every BlockHandler. So whatever is
*in* the shadow buffer reaches the real framebuffer — the reparented client's pixels were
simply never drawn into it.

A source-level review of `ReparentWindow` (`dix/window.c`), the mi clip computation
(`mi/mivaltree.c`, `mi/miwindow.c`, `mi/miexpose.c`), the damage wiring
(`miext/damage/damage.c`) and the GC composite-clip revalidation (`fb/fbgc.c`) found the
stock 21.1.15 logic correct on paper. The one structural difference between the working
(xwm, 1-level) and broken (matchbox, 2-level `root → frame → client`) cases is the deeper
nesting, which exercises the region (pixman) code more heavily. That pointed the finger at
the clip computation producing an **empty clip region** for the reparented client — which
explains everything: an empty `clipList` means the client's Expose region is empty (it is
never painted) and any drawing it does is clipped to nothing.

### 2.3 Fix

A conservative defensive fallback in `miComputeClips` (`mi/mivaltree.c`), in the spirit of
the existing full-screen `shadowRedisplay` patch (correctness over speed):

```c
    /* Asterinas RISC-V bring-up: under the shadow fbdev driver a client reparented
     * into a WM frame can end up with an empty clip region, so it is never painted
     * or exposed. Fall back to the window's border area. */
    if (!RegionNotEmpty(universe) && RegionNotEmpty(&pParent->borderSize))
        RegionCopy(universe, &pParent->borderSize);
```

The xserver was rebuilt (`ninja` in `target/riscv-cross/src/xserver/build`), and the
stripped `Xorg` reinstalled. This patch lives in the xserver source tree under
`target/riscv-cross/` (not in this repo's git), alongside the two existing local patches
(`fbdevhw.c` sysfs skip, `shadow.c` full-screen blit).

**Result:** with this patch matchbox-wm renders reparented client content. The M2
screenshot (`target/qemu-uboot/riscv-gtk2-m2-desktop.ppm`, 1280×1024) pixel histogram:

| color | pixels | meaning |
|---|---|---|
| `#DCDAD5` | 97% | two `gtk-hello` client windows (matchbox tiles them) |
| `#496179` | 1.5% | matchbox-wm frame **titlebars** |
| `#697D96` / `#384961` | 0.2% | matchbox-wm frame bevel (titlebar shading) |
| `#202028` | 0.2% | xpanel bar |
| `#E0E0E0` | 0.07% | xpanel clock digits |

Serial log confirms the session: two `gtk-hello: window mapped, entering main loop`,
`matchbox: failed to load keyboard config` (the non-fatal config warning, after one
retry for the X-not-ready case), and `xpanel: panel up`.

> **Note:** this is a bring-up stopgap. The stock clip logic is correct, so the true root
> cause is most likely in the region (pixman) code under 2-level nesting on RISC-V, or a
> RISC-V-specific fb fill path; the proper fix is to instrument `miComputeClips` /
> `miWindowExposures` and bisect the region code. Left for future work.

---

## 3. File manager evaluation — pcmanfm cross-compiles, but is too large to bundle

### 3.1 lxpanel — still blocked (unchanged from M1)

`lxpanel` is a GTK2 app with a `.so` plugin architecture, blocked for the same reason as
`matchbox-panel`: a static build has no `dlopen`, so `g_module_open`-loaded plugins can't
work. Not pursued further.

### 3.2 pcmanfm — monolithic, builds successfully

`pcmanfm 1.3.2` (the last GTK2 release) is monolithic (no plugin model), so it links as a
single static GTK2 binary exactly like `gtk-hello`. Its dependency chain is
`libfm-extra → menu-cache → libfm (full) → pcmanfm`:

| package | version | notes |
|---|---|---|
| libfm-extra | (libfm 1.3.2, `--with-extra-only`) | glib-only utility lib; needed by menu-cache |
| menu-cache | 1.1.1 | `libmenu-cache`; hard dep of full libfm |
| libfm | 1.3.2 | `libfm` + `libfm-gtk`; `src/modules` gio plugins dropped (shared-only) |
| pcmanfm | 1.3.2 | monolithic file manager |

The recipe is `tools/riscv/xorg/build_pcmanfm.sh` and reuses the established GTK2
cross-compile environment (static cross tree, `pkg-config-static` wrapper, GCC 15
`-Wno-*` + `-fcommon`). Hard-won pitfalls, all baked into the script:

- **Host has no gtk-doc / intltool / glib-gettextize.** Stub `gtk-doc.m4` + `intltool.m4`
  (on `ACLOCAL_PATH`) and a no-op `intltoolize`/`gtkdocize` on `PATH` so `autoreconf`
  runs. The intltool `*_RULE` recipes use `cp $< $@`; note `$@` must be written `[$]@`
  inside the `AC_DEFUN` body or m4 expands it to the macro argument (`0.40.0`).
- **`gtk-doc.make`** is included unconditionally by the docs Makefile.ams and must define
  `EXTRA_DIST =` (but nothing else — defining `DISTCLEANFILES` etc. trips automake's
  `-Werror` "multiply defined").
- **`glib-genmarshal`** must be on `PATH` (the cross prefix's host-runnable Python script;
  symlinked into the autotools stub `bin/`).
- **`src/modules`** builds gio plugins with libtool `-module -shared`; a static build can't
  produce them, so the `modules`/`tests` subdirs are dropped from `SUBDIRS`.
- **`libfm.pc.in` omits `libmenu-cache`**, so a static pcmanfm link fails with undefined
  `menu_cache_*` symbols; appended to `Requires`.

**Result:** `pcmanfm` builds and links (15 MB stripped).

### 3.3 Why pcmanfm is not in the default session

`pcmanfm` (15 MB) plus the rest of the desktop crosses the kernel's initramfs
early-memory limit (§4). It builds cleanly and is ready to bundle once that kernel limit is
raised. Until then the default session keeps the two smaller GTK2 windows.

---

## 4. Kernel gap — initramfs early-memory limit (~20 MB gzipped)

This is the one *new* kernel-side gap hit this milestone (not yet fixed in-kernel):

- **Symptom:** with a gzipped initramfs above roughly 20 MB, the kernel prints
  `Spawn the first kernel thread` and then **hangs** — the serial line never reaches the
  `unpacking initramfs` message, so the stall is in the first-kernel-thread init path
  (`component::init_all` / the early memory-region setup), *before* the initramfs is even
  decompressed. The `[warn] userspace marker not reached` from `qemu_desktop_boot.py` is
  this (its 120 s deadline is unrelated; the boot genuinely does not proceed).
- **Threshold observed:** 23.7 MB (M1's full session incl. xterm) and 21.2 MB both stall;
  18.7 MB boots. The M1 memory note ("41 MB stalls, 18 MB works") is consistent.
- **Workaround applied here:** trim the initramfs under ~19 MB gzipped — drop the 12 MB
  full ncurses terminfo DB (keep only `x/` = xterm entries, ~0.5 MB), drop the superseded
  `xwm`/`xclient` demos, and leave `pcmanfm`/`xterm` out of the default session.
- **Proper fix (future):** the early initramfs memory region handling in OSTD/RISC-V needs
  to tolerate a larger `Module` region (or the gzip→ramfs path needs to stream rather than
  materialising the whole image). This is a downstream kernel gap, tracked here rather than
  upstream.

---

## 5. Session and artifacts

Default session (`tools/riscv/xorg/init.c`): Xorg → `matchbox-window-manager` →
`xpanel` → `gtk-hello` ×2. (`xterm`/`pcmanfm` build but are left out per §4.)

| file | what it is |
|---|---|
| `tools/riscv/xorg/GTK-M2-report.md` | this report |
| `tools/riscv/xorg/build_pcmanfm.sh` | pcmanfm cross-compile recipe (libfm-extra → menu-cache → libfm → pcmanfm) |
| `tools/riscv/xorg/init.c` | session: matchbox-wm + xpanel + 2× gtk-hello |
| `tools/riscv/xorg/build_xorg_initramfs.sh` | initramfs packaging (trimmed to fit the ~20 MB limit) |
| `tools/riscv/xorg/xwm_reparent.c` | minimal reparenting WM (isolation test for the shadow bug) |
| xserver `mi/mivaltree.c` | the clip-region fallback fix (in `target/riscv-cross/src/xserver`, not this repo's git) |
