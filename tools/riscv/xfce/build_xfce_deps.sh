#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# XFCE-M2 dependencies: rebuild the riscv64 cross prefix
# (target/riscv-cross/usr) with everything the six Xfce core libraries need.
#
# Background: the original cross prefix (systemd + Xorg + GTK2 + D-Bus from the
# M1 milestones) was lost when target/ was wiped, so this script rebuilds the
# subset needed to compile libxfce4util/xfconf/libxfce4ui/garcon/libwnck/exo.
# Unlike the M1 desktop clients (statically linked), these are built as shared
# libraries — that is how real distributions ship Xfce and how xfwm4/xfce4-panel
# will consume them in M3.
#
# Tiers (strict build order, each package idempotent via logs/.done-* markers):
#   A. base:      zlib libffi pcre2 expat libpng
#   B. X11:       xorgproto xtrans libXau xcb-proto libxcb libX11 libXext
#                 libXrender libXrandr libXcursor libXfixes libXcomposite
#                 libXdamage libXi libXtst libXres libICE libSM xcb-util
#                 startup-notification
#   C. glib:      glib (meson), then D-Bus via tools/riscv/systemd/build_dbus.sh
#   D. rendering: freetype fontconfig pixman fribidi harfbuzz cairo pango
#                 libxml2 at-spi2-core (provides atk/atspi/atk-bridge)
#                 gdk-pixbuf libepoxy gtk3
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/xfce_cross_env.sh"


# ---------------------------------------------------------------- tier A ----

build_zlib() {
  cd "$SRC"
  local d; d=$(srcdir zlib-1.3.1.tar.gz \
    https://github.com/madler/zlib/releases/download/v1.3.1/zlib-1.3.1.tar.gz \
    https://zlib.net/fossils/zlib-1.3.1.tar.gz)
  cd "$SRC/$d"
  ./configure --prefix="$PREFIX"
  make -j"$JOBS" && make install
}

build_libffi() {
  cd "$SRC"
  local d; d=$(srcdir libffi-3.4.7.tar.gz \
    https://github.com/libffi/libffi/releases/download/v3.4.7/libffi-3.4.7.tar.gz)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" --libdir="$PREFIX/lib" \
    --enable-shared --disable-static --disable-docs
  make -j"$JOBS" && make install
}

build_pcre2() {
  cd "$SRC"
  local d; d=$(srcdir pcre2-10.44.tar.bz2 \
    https://github.com/PCRE2Project/pcre2/releases/download/pcre2-10.44/pcre2-10.44.tar.bz2)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    --enable-pcre2-8 --disable-pcre2-16 --disable-pcre2-32
  make -j"$JOBS" && make install
}

build_expat() {
  cd "$SRC"
  local d; d=$(srcdir expat-2.7.1.tar.xz \
    https://github.com/libexpat/libexpat/releases/download/R_2_7_1/expat-2.7.1.tar.xz)
  cd "$SRC/$d"
  # shellcheck disable=SC2046
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    $(copt --without-docbook) $(copt --without-xmlwf) $(copt --without-examples) \
    $(copt --without-tests)
  make -j"$JOBS" && make install
}

build_libpng() {
  cd "$SRC"
  local d; d=$(srcdir libpng-1.6.47.tar.xz \
    https://download.sourceforge.net/libpng/libpng-1.6.47.tar.xz \
    https://deb.debian.org/debian/pool/main/libp/libpng1.6/libpng1.6_1.6.47.orig.tar.xz)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static
  make -j"$JOBS" && make install
}

# ---------------------------------------------------------------- tier B ----

XORG_LIB="https://www.x.org/releases/individual/lib"

x11_autotools() { # x11_autotools <tarball> <url> [extra configure flags...]
  local tar="$1" url="$2"; shift 2
  cd "$SRC"
  local d; d=$(srcdir "$tar" "$url")
  cd "$SRC/$d"
  # shellcheck disable=SC2046
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    $(copt --disable-specs) $(copt --without-fop) $(copt --disable-docs) \
    $(copt --disable-devel-docs) $(copt --without-lint) $(copt --disable-lint-library) \
    $(copt --disable-malloc0returnsnull) \
    "$@"
  make -j"$JOBS" && make install
}

build_xorgproto() {
  cd "$SRC"
  local d; d=$(srcdir xorgproto-2024.1.tar.xz \
    https://www.x.org/releases/individual/proto/xorgproto-2024.1.tar.xz)
  MESON_SETUP "$SRC/$d"
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_xtrans()    { x11_autotools xtrans-1.5.2.tar.xz       "$XORG_LIB/xtrans-1.5.2.tar.xz"; }
build_libXau()    { x11_autotools libXau-1.0.12.tar.xz      "$XORG_LIB/libXau-1.0.12.tar.xz"; }
build_xcb_proto() { x11_autotools xcb-proto-1.17.0.tar.xz \
  https://xorg.freedesktop.org/archive/individual/xcb/xcb-proto-1.17.0.tar.xz; }
build_libxcb() {
  # PKG_CONFIG_SYSROOT_DIR prepends the sysroot to xcb-proto.pc's datadir
  # variable, producing a doubled path for the XML protocol descriptions;
  # pass the real locations explicitly.
  cd "$SRC"
  local d; d=$(srcdir libxcb-1.17.0.tar.xz \
    https://xorg.freedesktop.org/archive/individual/xcb/libxcb-1.17.0.tar.xz)
  cd "$SRC/$d"
  # shellcheck disable=SC2046
  local pydir; pydir=$(echo "$PREFIX"/lib/python3*/site-packages)
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    $(copt --disable-specs) $(copt --without-fop) $(copt --disable-devel-docs) \
    $(copt --without-lint) \
    XCBPROTO_XCBINCLUDEDIR="$PREFIX/share/xcb" \
    XCBPROTO_XCBPYTHONDIR="$pydir"
  make -j"$JOBS" && make install
}

build_libX11() {
  x11_autotools libX11-1.8.12.tar.xz "$XORG_LIB/libX11-1.8.12.tar.xz" \
    --disable-malloc0returnsnull
}

build_libXext()       { x11_autotools libXext-1.3.6.tar.xz       "$XORG_LIB/libXext-1.3.6.tar.xz"; }
build_libXrender()    { x11_autotools libXrender-0.9.12.tar.xz   "$XORG_LIB/libXrender-0.9.12.tar.xz"; }
build_libXrandr()     { x11_autotools libXrandr-1.5.4.tar.xz     "$XORG_LIB/libXrandr-1.5.4.tar.xz"; }
build_libXcursor()    { x11_autotools libXcursor-1.2.3.tar.xz    "$XORG_LIB/libXcursor-1.2.3.tar.xz"; }
build_libXfixes()     { x11_autotools libXfixes-6.0.1.tar.xz     "$XORG_LIB/libXfixes-6.0.1.tar.xz"; }
build_libXcomposite() { x11_autotools libXcomposite-0.4.6.tar.xz "$XORG_LIB/libXcomposite-0.4.6.tar.xz"; }
build_libXdamage()    { x11_autotools libXdamage-1.1.6.tar.xz    "$XORG_LIB/libXdamage-1.1.6.tar.xz"; }
build_libXi()         { x11_autotools libXi-1.8.2.tar.xz         "$XORG_LIB/libXi-1.8.2.tar.xz"; }
build_libXtst()       { x11_autotools libXtst-1.2.5.tar.xz       "$XORG_LIB/libXtst-1.2.5.tar.xz"; }
build_libXres()       { x11_autotools libXres-1.2.2.tar.xz       "$XORG_LIB/libXres-1.2.2.tar.xz"; }
build_libICE()        { x11_autotools libICE-1.1.2.tar.xz        "$XORG_LIB/libICE-1.1.2.tar.xz"; }
build_libSM()         { x11_autotools libSM-1.2.6.tar.xz         "$XORG_LIB/libSM-1.2.6.tar.xz"; }
build_xcb_util()      { x11_autotools xcb-util-0.4.1.tar.xz \
  https://xorg.freedesktop.org/archive/individual/xcb/xcb-util-0.4.1.tar.xz; }

build_startup_notification() {
  cd "$SRC"
  local d; d=$(srcdir startup-notification-0.12.tar.gz \
    https://www.freedesktop.org/software/startup-notification/releases/startup-notification-0.12.tar.gz)
  cd "$SRC/$d"
  # 0.12 (2009) ships a config.sub/config.guess too old to know riscv64;
  # refresh them from the local automake installation.
  cp -f /usr/share/automake-*/config.sub /usr/share/automake-*/config.guess .
  # lf_cv_sane_realloc=yes: configure AC_TRY_RUN-probes realloc(NULL,); the
  # test cannot run when cross-compiling. glibc's realloc(NULL,n) is malloc.
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    lf_cv_sane_realloc=yes
  make -j"$JOBS" && make install
}

# ---------------------------------------------------------------- tier C ----

build_glib() {
  cd "$SRC"
  local d; d=$(srcdir glib-2.80.5.tar.xz \
    https://download.gnome.org/sources/glib/2.80/glib-2.80.5.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" libmount disabled) \
    $(mopt "$SRC/$d/meson_options.txt" selinux disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tests false) \
    $(mopt "$SRC/$d/meson_options.txt" introspection disabled) \
    $(mopt "$SRC/$d/meson_options.txt" nls disabled) \
    $(mopt "$SRC/$d/meson_options.txt" man-pages disabled) \
    $(mopt "$SRC/$d/meson_options.txt" documentation false) \
    $(mopt "$SRC/$d/meson_options.txt" gtk_doc false) \
    $(mopt "$SRC/$d/meson_options.txt" libelf disabled) \
    $(mopt "$SRC/$d/meson_options.txt" sysprof disabled) \
    $(mopt "$SRC/$d/meson_options.txt" dtrace false) \
    $(mopt "$SRC/$d/meson_options.txt" systemtap false) \
    $(mopt "$SRC/$d/meson_options.txt" force_posix_threads true)
  NINJA_INSTALL "$SRC/$d/build-riscv"
  # glib-2.0.pc/gio-2.0.pc advertise the GLib build tools as ${bindir}/... —
  # but $PREFIX/bin holds riscv64 target binaries that downstream meson builds
  # (gtk3, ...) would try to execute on the host. Point the pc variables at
  # the host's own GLib tools (yocto-style glib-native split).
  local pcv
  for pcv in glib_genmarshal:glib-genmarshal glib_mkenums:glib-mkenums; do
    sed -i "s|^${pcv%%:*}=.*|${pcv%%:*}=/usr/bin/${pcv##*:}|" "$PREFIX/lib/pkgconfig/glib-2.0.pc"
  done
  for pcv in glib_compile_schemas:glib-compile-schemas glib_compile_resources:glib-compile-resources \
             gdbus_codegen:gdbus-codegen gdbus:gdbus; do
    sed -i "s|^${pcv%%:*}=.*|${pcv%%:*}=/usr/bin/${pcv##*:}|" "$PREFIX/lib/pkgconfig/gio-2.0.pc"
  done
}

build_dbus() {
  # Reuses the M1 script verbatim (static libdbus-1 + dbus-daemon +
  # dbus-send/monitor/uuidgen/dbus-run-session in the cross prefix).
  bash "$ROOT/tools/riscv/systemd/build_dbus.sh"
}

# ---------------------------------------------------------------- tier D ----

build_freetype() {
  cd "$SRC"
  local d; d=$(srcdir freetype-2.13.3.tar.xz \
    https://download.savannah.gnu.org/releases/freetype/freetype-2.13.3.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" zlib enabled) \
    $(mopt "$SRC/$d/meson_options.txt" png enabled) \
    $(mopt "$SRC/$d/meson_options.txt" bzip2 disabled) \
    $(mopt "$SRC/$d/meson_options.txt" brotli disabled) \
    $(mopt "$SRC/$d/meson_options.txt" harfbuzz disabled)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_fontconfig() {
  cd "$SRC"
  local d; d=$(srcdir fontconfig-2.16.0.tar.xz \
    https://www.freedesktop.org/software/fontconfig/release/fontconfig-2.16.0.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" doc disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tests disabled) \
    $(mopt "$SRC/$d/meson_options.txt" nls disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tools enabled)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_pixman() {
  cd "$SRC"
  local d; d=$(srcdir pixman-0.44.2.tar.gz \
    https://www.cairographics.org/releases/pixman-0.44.2.tar.gz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" tests disabled) \
    $(mopt "$SRC/$d/meson_options.txt" demos disabled) \
    $(mopt "$SRC/$d/meson_options.txt" gtk disabled) \
    $(mopt "$SRC/$d/meson_options.txt" libpng enabled)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_fribidi() {
  cd "$SRC"
  local d; d=$(srcdir fribidi-1.0.16.tar.xz \
    https://github.com/fribidi/fribidi/releases/download/v1.0.16/fribidi-1.0.16.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" tests false) \
    $(mopt "$SRC/$d/meson_options.txt" docs false) \
    $(mopt "$SRC/$d/meson_options.txt" bin false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_harfbuzz() {
  cd "$SRC"
  local d; d=$(srcdir harfbuzz-8.5.0.tar.xz \
    https://github.com/harfbuzz/harfbuzz/releases/download/8.5.0/harfbuzz-8.5.0.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" glib enabled) \
    $(mopt "$SRC/$d/meson_options.txt" freetype enabled) \
    $(mopt "$SRC/$d/meson_options.txt" cairo disabled) \
    $(mopt "$SRC/$d/meson_options.txt" icu disabled) \
    $(mopt "$SRC/$d/meson_options.txt" graphite disabled) \
    $(mopt "$SRC/$d/meson_options.txt" introspection disabled) \
    $(mopt "$SRC/$d/meson_options.txt" docs disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tests disabled) \
    $(mopt "$SRC/$d/meson_options.txt" utilities disabled) \
    $(mopt "$SRC/$d/meson_options.txt" benchmark disabled) \
    $(mopt "$SRC/$d/meson_options.txt" chafa disabled)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_cairo() {
  cd "$SRC"
  local d; d=$(srcdir cairo-1.18.2.tar.xz \
    https://www.cairographics.org/releases/cairo-1.18.2.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" fontconfig enabled) \
    $(mopt "$SRC/$d/meson_options.txt" freetype enabled) \
    $(mopt "$SRC/$d/meson_options.txt" png enabled) \
    $(mopt "$SRC/$d/meson_options.txt" zlib enabled) \
    $(mopt "$SRC/$d/meson_options.txt" xcb enabled) \
    $(mopt "$SRC/$d/meson_options.txt" xlib enabled) \
    $(mopt "$SRC/$d/meson_options.txt" glib enabled) \
    $(mopt "$SRC/$d/meson_options.txt" spectre disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tests disabled) \
    $(mopt "$SRC/$d/meson_options.txt" gtk_doc false) \
    $(mopt "$SRC/$d/meson_options.txt" symbol-lookup disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tee disabled) \
    $(mopt "$SRC/$d/meson_options.txt" xml disabled) \
    $(mopt "$SRC/$d/meson_options.txt" gtk2-utils disabled)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_pango() {
  cd "$SRC"
  local d; d=$(srcdir pango-1.54.0.tar.xz \
    https://download.gnome.org/sources/pango/1.54/pango-1.54.0.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" cairo enabled) \
    $(mopt "$SRC/$d/meson_options.txt" fontconfig enabled) \
    $(mopt "$SRC/$d/meson_options.txt" freetype enabled) \
    $(mopt "$SRC/$d/meson_options.txt" xft disabled) \
    $(mopt "$SRC/$d/meson_options.txt" introspection disabled) \
    $(mopt "$SRC/$d/meson_options.txt" gtk_doc false) \
    $(mopt "$SRC/$d/meson_options.txt" build-testsuite false) \
    $(mopt "$SRC/$d/meson_options.txt" build-examples false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_libxml2() {
  cd "$SRC"
  local d; d=$(srcdir libxml2-2.13.5.tar.xz \
    https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.5.tar.xz)
  # libxml2's meson options: python/history/readline are booleans,
  # lzma/icu are features.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" python false) \
    $(mopt "$SRC/$d/meson_options.txt" lzma disabled) \
    $(mopt "$SRC/$d/meson_options.txt" icu disabled) \
    $(mopt "$SRC/$d/meson_options.txt" readline false) \
    $(mopt "$SRC/$d/meson_options.txt" history false) \
    $(mopt "$SRC/$d/meson_options.txt" docs disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tests false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
  # libxml2's meson install writes `Cflags: -I${includedir}` (missing the
  # libxml-2.0 subdir where headers actually live) — downstream consumers
  # (shared-mime-info, at-spi2-core tests) then fail to find libxml/parser.h.
  sed -i 's|^Cflags: .*|Cflags: -I${includedir}/libxml2|' \
    "$PREFIX/lib/pkgconfig/libxml-2.0.pc"
}

build_at_spi2_core() {
  cd "$SRC"
  local d; d=$(srcdir at-spi2-core-2.54.0.tar.xz \
    https://download.gnome.org/sources/at-spi2-core/2.54/at-spi2-core-2.54.0.tar.xz)
  # Since 2.50 at-spi2-core also ships ATK itself (atk.pc), plus libatspi and
  # the X11 atk-bridge module GTK3's configure hard-requires.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    "-Dc_args=-I$PREFIX/include -I$PREFIX/include/libxml2" \
    $(mopt "$SRC/$d/meson_options.txt" introspection disabled) \
    $(mopt "$SRC/$d/meson_options.txt" docs false) \
    $(mopt "$SRC/$d/meson_options.txt" x11 enabled) \
    $(mopt "$SRC/$d/meson_options.txt" atk_only false) \
    $(mopt "$SRC/$d/meson_options.txt" use_systemd false) \
    $(mopt "$SRC/$d/meson_options.txt" gtk2_atk_adaptor false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_shared_mime_info() {
  cd "$SRC"
  local d; d=$(srcdir shared-mime-info-2.4.tar.gz \
    https://gitlab.freedesktop.org/xdg/shared-mime-info/-/archive/2.4/shared-mime-info-2.4.tar.gz \
    https://deb.debian.org/debian/pool/main/s/shared-mime-info/shared-mime-info_2.4.orig.tar.gz)
  # Mostly a data package (freedesktop.org.xml); the C tool
  # update-mime-database only needs glib + libxml2, both in the prefix.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" update-mimedb true) \
    $(mopt "$SRC/$d/meson_options.txt" translations false) \
    $(mopt "$SRC/$d/meson_options.txt" tests false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_gdk_pixbuf() {
  cd "$SRC"
  local d; d=$(srcdir gdk-pixbuf-2.42.12.tar.xz \
    https://download.gnome.org/sources/gdk-pixbuf/2.42/gdk-pixbuf-2.42.12.tar.xz)
  # builtin_loaders=png,xpm: avoids the runtime gdk-pixbuf-query-loaders
  # loaders.cache dance (the generator is a target binary that cannot run on
  # the build host). png for icons; xpm because xfwm4's bundled window-frame
  # themes are all .xpm — without the loader xfwm4 draws no decorations.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" png enabled) \
    $(mopt "$SRC/$d/meson_options.txt" jpeg disabled) \
    $(mopt "$SRC/$d/meson_options.txt" tiff disabled) \
    $(mopt "$SRC/$d/meson_options.txt" introspection disabled) \
    $(mopt "$SRC/$d/meson_options.txt" man false) \
    $(mopt "$SRC/$d/meson_options.txt" tests false) \
    $(mopt "$SRC/$d/meson_options.txt" installed_tests false) \
    $(mopt "$SRC/$d/meson_options.txt" builtin_loaders png,xpm) \
    $(mopt "$SRC/$d/meson_options.txt" relocatable false) \
    $(mopt "$SRC/$d/meson_options.txt" gtk_doc false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_libepoxy() {
  cd "$SRC"
  local d; d=$(srcdir libepoxy-1.5.10.tar.xz \
    https://download.gnome.org/sources/libepoxy/1.5/libepoxy-1.5.10.tar.xz)
  # No EGL in the guest (fbdev Xorg, no GPU), but GLX must stay enabled:
  # gtk3's X11 backend includes <epoxy/glx.h> unconditionally. epoxy ships
  # its own generated dispatch headers, so no Mesa headers are needed.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" docs false) \
    $(mopt "$SRC/$d/meson_options.txt" tests false) \
    $(mopt "$SRC/$d/meson_options.txt" glx yes) \
    $(mopt "$SRC/$d/meson_options.txt" egl no) \
    $(mopt "$SRC/$d/meson_options.txt" x11 true)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_gtk3() {
  cd "$SRC"
  local d; d=$(srcdir gtk+-3.24.43.tar.xz \
    https://download.gnome.org/sources/gtk+/3.24/gtk+-3.24.43.tar.xz)
  # gtk+ 3.24.43 (the final 3.24 release) is Meson-only — autotools support
  # was removed in this version. X11 backend only; host-side GLib tools
  # (glib-compile-resources, gdbus-codegen, glib-mkenums) are picked up from
  # PATH as needed.
  # The gdbus-codegen in use is the host's (see build_glib note); host glib
  # (2.88) emits g_variant_builder_init_static(), which only exists in
  # glib >= 2.84, while the target glib is 2.80. The _static variant is a
  # drop-in performance alias of g_variant_builder_init() — shim it.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    "-Dc_args=-I$PREFIX/include -Dg_variant_builder_init_static=g_variant_builder_init" \
    $(mopt "$SRC/$d/meson_options.txt" x11_backend true) \
    $(mopt "$SRC/$d/meson_options.txt" wayland_backend false) \
    $(mopt "$SRC/$d/meson_options.txt" broadway_backend false) \
    $(mopt "$SRC/$d/meson_options.txt" win32_backend false) \
    $(mopt "$SRC/$d/meson_options.txt" quartz_backend false) \
    $(mopt "$SRC/$d/meson_options.txt" xinerama no) \
    $(mopt "$SRC/$d/meson_options.txt" cloudproviders false) \
    $(mopt "$SRC/$d/meson_options.txt" tracker3 false) \
    $(mopt "$SRC/$d/meson_options.txt" colord no) \
    $(mopt "$SRC/$d/meson_options.txt" print_backends file) \
    $(mopt "$SRC/$d/meson_options.txt" gtk_doc false) \
    $(mopt "$SRC/$d/meson_options.txt" man false) \
    $(mopt "$SRC/$d/meson_options.txt" introspection false) \
    $(mopt "$SRC/$d/meson_options.txt" demos false) \
    $(mopt "$SRC/$d/meson_options.txt" examples false) \
    $(mopt "$SRC/$d/meson_options.txt" tests false) \
    $(mopt "$SRC/$d/meson_options.txt" installed_tests false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

# ------------------------------------------------------------------ main ----

run_pkg zlib                  build_zlib
run_pkg libffi                build_libffi
run_pkg pcre2                 build_pcre2
run_pkg expat                 build_expat
run_pkg libpng                build_libpng

run_pkg xorgproto             build_xorgproto
run_pkg xtrans                build_xtrans
run_pkg libXau                build_libXau
run_pkg xcb-proto             build_xcb_proto
run_pkg libxcb                build_libxcb
run_pkg libX11                build_libX11
run_pkg libXext               build_libXext
run_pkg libXrender            build_libXrender
run_pkg libXrandr             build_libXrandr
run_pkg libXfixes             build_libXfixes
run_pkg libXcursor            build_libXcursor
run_pkg libXcomposite         build_libXcomposite
run_pkg libXdamage            build_libXdamage
run_pkg libXi                 build_libXi
run_pkg libXtst               build_libXtst
run_pkg libXres               build_libXres
run_pkg libICE                build_libICE
run_pkg libSM                 build_libSM
run_pkg xcb-util              build_xcb_util
run_pkg startup-notification  build_startup_notification

run_pkg glib                  build_glib
run_pkg dbus                  build_dbus

run_pkg freetype              build_freetype
run_pkg fontconfig            build_fontconfig
run_pkg pixman                build_pixman
run_pkg fribidi               build_fribidi
run_pkg harfbuzz              build_harfbuzz
run_pkg cairo                 build_cairo
run_pkg pango                 build_pango
run_pkg libxml2               build_libxml2
run_pkg at-spi2-core          build_at_spi2_core
run_pkg shared-mime-info      build_shared_mime_info
run_pkg gdk-pixbuf            build_gdk_pixbuf
run_pkg libepoxy              build_libepoxy
run_pkg gtk3                  build_gtk3

echo "=== all XFCE-M2 dependencies built ==="
