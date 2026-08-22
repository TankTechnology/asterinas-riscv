# XFCE-M2 — Xfce core library cross-compile matrix

Date: 2026-08-22
Status: **MILESTONE ACHIEVED** — all six Xfce core libraries cross-compile for
riscv64 and are packaged into the systemd desktop initramfs pipeline. No guest
boot is claimed (that is M3).

## 1. Scope and approach

Task: cross-compile `libxfce4util → xfconf → libxfce4ui → garcon → libwnck →
exo` to riscv64 in dependency order and get them into the rootfs/initramfs
packaging pipeline.

**Version selection: Xfce 4.20.0** (plus libwnck 43.3 from GNOME). 4.20 was
chosen over 4.18 because the 4.18 series still requires `intltool` + Perl
XML::Parser for its autotools builds, and the host has neither; 4.20 migrated
project-wide to plain gettext, so no intltool is needed anywhere.

**Shared, not static.** The M1 desktop clients (matchbox/pcmanfm/netsurf) were
statically linked. The Xfce libraries are built as shared libraries — that is
how distributions ship them and how the M3 components (xfwm4, xfce4-panel,
xfdesktop) consume them. The guest glibc loader searches `/usr/lib` by default
(proven by `libxcvt.so.0` resolving there in the M1 image), so no ldconfig
cache is needed.

**The cross prefix was gone.** `target/riscv-cross` (systemd + Xorg + GTK2 +
D-Bus from the earlier milestones) was lost when `target/` was wiped again, so
`build_xfce_deps.sh` first rebuilds the entire dependency subset the six
libraries need — 38 packages in four tiers (base libs → X11 client libs →
glib/D-Bus → GTK3 stack). Everything is idempotent (`logs/.done-<pkg>`
markers) and re-runnable; tarballs are mirrored to
`~/Program/backups/xfce-m2-tarballs/` because `target/` is volatile. The
resulting prefix is backed up to `~/Program/backups/xfce-m2/`.

## 2. Result matrix

| package | version | build system | one-shot? | notes |
|---|---|---|---|---|
| libxfce4util | 4.20.0 | autotools (fallback) | no | tarball's Meson build is incomplete (subdir `meson.build` files not dist'd); also Meson setup unconditionally requires the host tool `xdt-gen-visibility` (xfce4-dev-tools, built host-side). Autotools build works unchanged. |
| xfconf | 4.20.0 | autotools | no | one issue: host `gdbus-codegen` (glib 2.88) emits `g_variant_builder_init_static()` (glib ≥ 2.84) but the target glib is 2.80 — fixed with a global `-Dg_variant_builder_init_static=g_variant_builder_init` shim (drop-in alias). GDBus-based since 4.20; no dbus-glib needed. |
| libxfce4ui | 4.20.0 | autotools | yes (after deps) | found gtk3, libxfconf, libSM/ICE, startup-notification, epoxy-adjacent stack; optional libgtop/gudev/gladeui autodetected off |
| garcon | 4.20.0 | autotools | yes | needs gtk3 + libxfce4ui (hence the task's build order is correct) |
| libwnck | 43.3 | meson | yes | gtk3, libX11, libXi, libXres, startup-notification |
| exo | 4.20.0 | autotools | yes | needs gtk3 + libxfce4ui |

Verification: all seven shared libraries + six pkg-config files present and
correct in `target/riscv-cross/usr` (script's self-check), `NEEDED` closure of
every Xfce library and of `xfconfd`/`xfce4-about`/`exo-open` resolves inside
the staged rootfs (glibc base + `/usr/lib` payload) — see §4.

## 3. Dependency tree built (38 packages)

Tier A: zlib 1.3.1, libffi 3.4.7, pcre2 10.44, expat 2.7.1, libpng 1.6.47.
Tier B (X11): xorgproto 2024.1, xtrans 1.5.2, libXau 1.0.12, xcb-proto
1.17.0, libxcb 1.17.0, libX11 1.8.12, libXext 1.3.6, libXrender 0.9.12,
libXrandr 1.5.4, libXfixes 6.0.1, libXcursor 1.2.3, libXcomposite 0.4.6,
libXdamage 1.1.6, libXi 1.8.2, libXtst 1.2.5, libXres 1.2.2, libICE 1.1.2,
libSM 1.2.6, xcb-util 0.4.1, startup-notification 0.12.
Tier C: glib 2.80.5 (meson), dbus 1.16.2 (reuses M1 `build_dbus.sh`, static
daemon + tools).
Tier D: freetype 2.13.3, fontconfig 2.16.0, pixman 0.44.2, fribidi 1.0.16,
harfbuzz 8.5.0, cairo 1.18.2, pango 1.54.0, libxml2 2.13.5, at-spi2-core
2.54.0 (provides atk/atspi/atk-bridge), shared-mime-info 2.4, gdk-pixbuf
2.42.12 (`builtin_loaders=png`), libepoxy 1.5.10 (glx=yes, egl=no), gtk+
3.24.43 (meson — the final 3.24 release dropped autotools).

Cross-compile pitfalls fixed along the way (all encoded in the scripts):

- **No `PKG_CONFIG_SYSROOT_DIR`.** Our `.pc` files bake the real final prefix;
  with a sysroot set, `pkg-config --variable=` consumers get a doubled path
  (libxcb's build needs xcb-proto's `xcbincludedir` verbatim).
- **`-Wl,-rpath-link` is mandatory.** This cross ld does not consult `-rpath`
  when resolving transitive `DT_NEEDED` of shared libraries given by path
  (glib's own executables could not find libpcre2/libffi without it).
- **Host vs target GLib tools.** `glib-2.0.pc`/`gio-2.0.pc` advertise
  `glib-compile-resources`/`gdbus-codegen`/... as `${bindir}/...`, i.e. the
  riscv64 binaries just installed — downstream meson builds (gtk3) then fail
  with `Exec format error`. `build_glib` rewrites those pc variables to the
  host's `/usr/bin` tools (yocto-style native split). The version skew this
  introduces (host glib 2.88 vs target 2.80) is what the
  `g_variant_builder_init_static` shim above fixes.
- **libxml2's meson `.pc` bug**: `Cflags: -I${includedir}` misses the
  `libxml2` subdir where headers live — sed-fixed post-install.
- **Ancient autotools**: startup-notification 0.12 ships a 2009 `config.sub`
  (no riscv64) and an `AC_TRY_RUN` realloc probe — refreshed config scripts +
  `lf_cv_sane_realloc=yes`.
- **Cross run-probes**: X libs need `--disable-malloc0returnsnull` (their
  malloc(0) probe cannot run under cross).
- **cairo needs glib enabled**: gtk3 requires `cairo-gobject`.
- **epoxy needs glx=yes**: gtk3's X11 backend includes `<epoxy/glx.h>`
  unconditionally; epoxy ships its own dispatch headers so no Mesa is needed.
- **gtk 3.24.43 is meson-only** (autotools removed in the final 3.24 release).

## 4. Packaging

`tools/riscv/xfce/pack_xfce_initramfs.sh` layers the Xfce payload onto the
backup M1 initramfs
(`~/Program/backups/asterinas-desktop-20260820/systemd-desktop-initramfs.cpio`
— used because the systemd build tree was wiped together with the prefix) and
emits `target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio` (147 MB raw
newc cpio). The same payload steps are also integrated as **step 8d of
`tools/riscv/systemd/build_systemd_desktop.sh`** (guarded on the libs being
present), so the full pipeline picks them up once the systemd build tree is
restored.

Payload: shared-library closure (`usr/lib/*.so*`, stripped), `xfconfd` (from
`lib/xfce4/xfconf/`), `xfce4-about`, exo tools, at-spi2 helpers
(`libexec/at-spi-bus-launcher`, `at-spi2-registryd`), D-Bus activation files
(`org.xfce.Xfconf.service`, a11y bus) with host-prefix `Exec=` paths rewritten
to guest paths, package data/icons/desktop files, the shared-mime-info
database, and gsettings schemas with `gschemas.compiled` generated at pack
time by the **host** `glib-compile-schemas` (output is arch-independent).

Closure check (script in §Verification): every `DT_NEEDED` of the seven Xfce
libraries and of `xfconfd`/`xfce4-about`/`exo-open`/at-spi2 helpers resolves
to a file inside the image. `org.xfce.Xfconf.service` points at
`/usr/lib/xfce4/xfconf/xfconfd`.

## 5. Kernel / userland gap list

M2 is host-side compilation, so it does not exercise the guest kernel; nothing
new surfaced here. The gaps below are inherited from earlier reports and
directly gate M3 (xfwm4 session):

| gap | evidence | impact on Xfce | status |
|---|---|---|---|
| `rseq` (syscall 293) unimplemented | glibc startup probe warnings; glib-stack crashes in GTK-M1 | glib 2.80 may hit the same instability; the earlier signal-11 crashes (openbox, lxpanel, glib 2.74) must be re-tested under glib 2.80 + dynamic linking | **in progress** (uncommitted `kernel/src/syscall/rseq.rs` in this worktree, other session) |
| `riscv_hwprobe` (258) unimplemented | glibc startup probes (ENOSYS fallback) | harmless warnings | **in progress** (uncommitted `riscv_hwprobe.rs`) |
| AF_NETLINK unimplemented | udev "Failed to start device monitor: Protocol not available" (DEMO-M1) | libudev-based hotplug (xfce4-settings, panel plugins) will degrade | open |
| tty VLNEXT/VSUSP unimplemented | KEYBOARD-M1 | cosmetic in xterm | open |
| zune-inflate hangs on >16 MB gzipped initramfs | build_systemd_desktop.sh header | initramfs must stay raw cpio (ours is, 147 MB — loads fine per M1) | worked around |
| initramfs early-memory limit (~20 MB gzipped) | GTK-M2 §4 | same as above | worked around |
| waitid `WEXITED` warning | BROWSER-M9 | cosmetic; systemd still reads exit status | open |

No kernel gap found in M2 was small enough to fix inline; none was needed to
compile or package the libraries.

## 6. M3 prerequisites (start xfwm4 desktop)

Libraries from M2 satisfy the link-time base. Still missing for a real
xfwm4-driven session:

1. **xfwm4 4.20** itself (gtk3, libxfce4ui, libxfconf, libwnck — all now in
   the prefix; libXpresent optional).
2. **Session components**: xfce4-session (SM/ICE now present), xfsettingsd /
   xfce4-settings (needs libXcursor — present; libxklavier and upower are
   optional), xfce4-panel (4.20 additionally needs **libxfce4windowing**,
   which wraps libwnck on X11), xfdesktop (libnotify optional).
3. **Session D-Bus bus**: M1 proved the system bus; xfconfd and at-spi2 run
   on the *session* bus — needs `dbus-run-session` (already shipped) wired
   into the session unit.
4. **Runtime data**: icon theme (only hicolor base shipped), an Xcursor
   theme, and a GTK theme (gtk3 default Adwaita is built-in).
5. **Re-test the glib-stack stability** (§5, rseq row) as the first runtime
   gate — the historical openbox/lxpanel crashes were on glib 2.74 with
   static linking; this stack is glib 2.80 shared.

## 7. Files

| file | purpose |
|---|---|
| `tools/riscv/xfce/xfce_cross_env.sh` | shared cross environment (prefix, pkg-config wrappers, meson cross file, fetch mirror, idempotent markers) |
| `tools/riscv/xfce/build_xfce_deps.sh` | 38-package dependency build (4 tiers) |
| `tools/riscv/xfce/build_xfce_libs.sh` | the six Xfce libraries + host xfce4-dev-tools bootstrap + artifact self-check |
| `tools/riscv/xfce/pack_xfce_initramfs.sh` | overlay packer → `systemd-desktop-xfce-initramfs.cpio` |
| `tools/riscv/systemd/build_systemd_desktop.sh` | step 8d: same Xfce payload in the full pipeline (guarded) |
| `~/Program/backups/xfce-m2-tarballs/` | all source tarballs (target/ is volatile) |
| `~/Program/backups/xfce-m2/` | cross prefix + produced initramfs backup |

## 8. Reproduce

```bash
bash tools/riscv/xfce/build_xfce_deps.sh   # idempotent; ~20 min warm
bash tools/riscv/xfce/build_xfce_libs.sh   # six libs + self-check
bash tools/riscv/xfce/pack_xfce_initramfs.sh
```
