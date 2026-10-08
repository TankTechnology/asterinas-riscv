# GTK M1 — GTK2 renders on the Asterinas RISC-V Xorg desktop

> **Historical milestone.** The SMP4 process-spawn conclusion in §6 was
> superseded by later four-hart boot and regression results. Do not use its
> SMP1 fallback as a current admission criterion.

**Status:** milestone reached + desktop-stack evaluation
**Date:** 2026-08-14
**Scope:** cross-compile glib → GTK2 for `riscv64`, render a GTK2 app on Xorg in QEMU, then evaluate LXDE vs Matchbox and pick the lighter desktop to push forward.

---

## 1. Milestone summary

M1 is **done**: a GTK2 window (label + text) renders correctly on Xorg 21.1.15
running on the Asterinas RISC-V framebuffer. The proof is
`tools/riscv/xorg/gtk-hello.c` — a minimal `gtk_window` + `gtk_label` +
`gtk_main` program, statically linked against the whole glib/GTK2 stack,
bundled into the initramfs, spawned by `init.c`, and verified via serial log
(`gtk-hello: window mapped, entering main loop`) plus screendump pixel analysis
(480×160 grey window, 409 dark text pixels).

The full cross-compiled stack (all static `.a` under `target/riscv-cross/usr/`,
toolchain `riscv64-linux-gnu-gcc` 15.1.0):

| stage | packages (version) | notes |
|-------|--------------------|-------|
| X11 base | libX11 libxcb xorgproto freetype fontconfig zlib libexpat pixman | prerequisite chain |
| **glib** | pcre2-10.47, libffi, **glib-2.74.7** | meson cross+native, `-Dnls=disabled` etc. |
| text | libpng-1.6.43, cairo-1.18.0, fribidi-1.0.16, harfbuzz-8.3.1, pango-1.50.14 | pangocairo closure = 34 libs |
| **GTK2** | libjpeg-turbo-3.0.4, gdk-pixbuf-2.42.12, atk-2.38.0, **gtk+-2.24.33** | autotools, `--disable-rebuilds` |

Static link closure for a GTK2 app is 45 `-l` flags
(`pkg-config --static --libs gtk+-2.0`), ending in the usual glibc
`dlopen/getpwnam/getaddrinfo` NSS warnings, which are harmless.

## 2. Why the milestone matters

`gtk-hello` proves the *runtime* path, not just the link. The initramfs ships
the static GTK2 binary, a font (`AdwaitaSans-Regular.ttf`) + `fonts.conf`, and
`init.c` sets `FONTCONFIG_FILE` and `HOME` so pango/fontconfig resolve text
without NSS. With a static glibc there is no NSS, so `getpwuid_r` fails and GTK2
warns "Could not find home directory" unless `HOME` is exported.

Key hard-won pitfalls already baked into the scripts (see memory
`riscv-desktop-cross-compile`):

- **Static GTK2 binaries are huge** (80 MB unstripped). The initramfs ballooned
  to 41 MB and the kernel stalled at "Spawn the first kernel thread" until the
  binary was stripped to 14 MB (initramfs 18 MB). **Always strip static GTK
  apps.**
- **meson static closures don't resolve `Requires.private`** — cairo→xcb→xau
  dropped `-lXau`; fixed with the `pkg-config-static` wrapper (`pkg-config
  --static`) wired into the meson cross file's `[binaries] pkgconfig`.
- **GTK2 cross** needs `--disable-rebuilds` (else it runs the target
  `gtk-update-icon-cache` on the host → exec format error), a whole set of GCC 15
  `-Wno-*` suppressions, and dropping the `perf` benchmark subdir (marshaler
  symbol clash under static link).
- **Slow boot**: Xorg full init is ~170 s in QEMU (shadow/fbdev dlopen), so
  `qemu_desktop_boot.py` must screendump at ~250 s, not its default 7 s.

## 3. Desktop-stack evaluation (step 4)

The goal was to put a *lightweight desktop* on top of Xorg. Two candidates were
evaluated: **LXDE** (`lxpanel`/`pcmanfm`) and **Matchbox**.

### 3.1 LXDE — blocked

`lxpanel` is a GTK2 application with a `.so` plugin architecture (each panel
plugin is a shared object loaded at runtime). `pcmanfm` is GTK2 but monolithic.
Both sit on exactly the glib/GTK2 stack that already **crashed openbox 3.6.1 at
runtime** (signal 11 null deref, `Unimplemented syscall 293`/`rseq`, glib 2.74 API
incompat, ~15 retries; disabling rseq via `GLIBC_TUNABLES` did not help). Given
the plugin `.so` requirement plus the glib-stack instability, LXDE is not the
lighter path here.

### 3.2 Matchbox — WM works, panel blocked

- **matchbox-window-manager 1.2.2 works.** It is *pure X11* (libX11/libXext
  only, no glib/pango/cairo), which is why it sidesteps the openbox crash. It
  is the WM of the current desktop session (see `init.c`).
- **matchbox-panel (GTK2) is blocked** — see §4.

### 3.3 Decision: pure X11 is the lighter path

The lighter, proven-working path in *this* environment is **pure X11**, not the
GTK2 desktop shells. Concretely:

| component | stack | status |
|-----------|-------|--------|
| matchbox-window-manager | pure X11 | ✅ works |
| xterm | Xaw (X11) | ✅ works (after 3 PTY kernel fixes) |
| gtk-hello | GTK2 + glib | ✅ works (simple) |
| openbox | glib/pango/cairo | ❌ crashes |
| lxpanel / matchbox-panel | GTK2 + `.so` plugins | ❌ blocked (see §4) |

---

## 4. matchbox-panel build attempt (concrete result)

I attempted the GTK2 `matchbox-panel` (`matchbox-panel-2` repo, `gtk2` tag =
v2.0, since tag `2.11` is the GTK3 rewrite which we deliberately did not build).
Recipe captured in `build_matchbox_panel.sh`.

**What worked:**

- `configure` resolved every dependency from our static tree:
  `glib-2.0 x11 gdk-x11-2.0 gtk+-2.0 (>=2.18) gmodule-export-2.0` — all *yes*.
- The panel **core** binary builds and links statically
  (`matchbox-panel/matchbox-panel`, 80 MB → 14.2 MB stripped).

**What blocks it (architectural, not a missing symbol):**

1. Applets are libtool *modules* — `applet_LTLIBRARIES = libclock.la` … with
   `-avoid-version -module` — i.e. `.so` plugins loaded at runtime via
   `g_module_open()`. Under `--disable-shared` they become static `.a` archives
   that cannot be dlopen'ed.
2. `matchbox-panel/mb-panel.c:287` exits before `gtk_main()` unless
   `g_module_supported()` is true, and that returns **false** for a fully-static
   glibc binary (no `dlopen`). The static panel core prints
   "gmodule support not found … required for matchbox-panel to work" and returns
   `-1`.

So a real matchbox-panel (or `lxpanel`, which has the same GTK2 plugin model)
would require rebuilding the **entire glib/GTK2 stack as shared libraries** —
plus it would still sit on the glib stack that crashed openbox. That is a
multi-hour rework with uncertain payoff, so it is **not** the lighter path.

---

## 5. Recommendation / next steps

1. **Stay on the pure-X11 path.** `matchbox-window-manager` + `xterm` +
   hand-written X11 clients (`xwm`, `xclient`) is a real, working lightweight
   desktop. It avoids both the shared-lib rework and the glib-stack crashes.
2. **Add a pure-X11 panel** (taskbar + clock + launcher) as a small static X11
   program, in the style of the existing `xwm.c`/`xclient.c`. This is the
   concrete "push the lighter desktop forward" — functionally what
   matchbox-panel provides, but implemented over libX11 only, so it is
   guaranteed to run (see `xpanel.c`, to be added).
3. **Revisit a shared-GTK build only if a GTK2 app becomes a hard requirement.**
   If so, rebuild glib/pango/cairo/gdk-pixbuf/atk/gtk2 with `--enable-shared`
   and `default_library=shared`, then retry matchbox-panel — but expect to also
   fix the openbox-style glib runtime crash first.

## 6. Artifacts

- `tools/riscv/xorg/gtk-hello.c` — M1 proof app (renders).
- `tools/riscv/xorg/xpanel.c` — pure-X11 panel (7-seg clock + launcher), renders.
- `tools/riscv/xorg/init.c` — desktop session (Xorg + xwm + xpanel + gtk-hello +
  xterm).
- `tools/riscv/xorg/build_libx_ext.sh` — libX* extensions.
- `tools/riscv/xorg/build_xorg_initramfs.sh` — initramfs packaging.
- `tools/riscv/xorg/build_matchbox_panel.sh` — GTK2 panel recipe + blocker doc.
- `tools/riscv/xorg/fonts.conf`, `xorg.conf` — font + X server config.

## 7. Session outcome — pure-X11 panel renders

The "lighter desktop" push landed as a **pure-X11 panel** (`xpanel.c`): a dark
bar across the top of the screen with a 7-segment digital clock (HH:MM:SS,
refreshed every second) and a "start" button that spawns `xterm`. It uses only
`libX11` primitives (digits are drawn as rectangles, no X core fonts needed),
so it runs the same way `xwm`/`xclient` do. Statically linked, 1.56 MB stripped.

Verified in QEMU via screendump pixel analysis (the full desktop renders):

| color | pixels | meaning |
|-------|--------|---------|
| `#303038` | 76.5% | `xwm` root background |
| `#ffffff` | 11.6% | `xterm` window |
| `#dcdad5` | 8.4% | `gtk-hello` window |
| `#202028` | 2.4% | **xpanel bar** |
| `#e0e0e0` | 0.1% | **xpanel clock digits** |
| `#0060c0` | 0.7% | `xwm` window borders |

Two findings worth recording:

1. **matchbox-window-manager breaks client-content rendering under the shadow
   driver.** It runs and draws its frame titlebar (`#496179`), but the reparented
   client window content never reaches the framebuffer — the desktop shows only
   a white/grey root + one titlebar. `xwm` (which reparents nothing, just adds a
   border) renders all client content correctly. So the session WM is switched
   back to `xwm`; the matchbox-wm frame-reparenting-vs-shadow issue is left as a
   future xserver investigation.

2. **`-smp 4` stalls the downstream kernel before init.** With `-smp 4` the
   kernel reaches `rootfs is ready` but pid-1 `/init` never runs (no serial
   marker after 6 min). `-smp 1` boots fine. This contradicts the upstream
   "SMP=4 passes" claim and is a downstream RISC-V SMP process-spawning gap to
   fix; `qemu_desktop_boot.py` therefore stays at `-smp 1` for now.
