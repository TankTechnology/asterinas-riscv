#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# XFCE-M2: cross-compile the six Xfce core libraries for riscv64 in dependency
# order: libxfce4util -> xfconf -> libxfce4ui -> garcon -> libwnck -> exo.
#
# Requires the dependency prefix from build_xfce_deps.sh (glib, GTK3, X11,
# startup-notification, D-Bus, ...). Xfce 4.20.0 tarballs are autotools with
# plain gettext (the intltool dependency was dropped project-wide in the 4.20
# cycle) except libxfce4util, which is Meson, and libwnck 43 (GNOME, Meson).
#
# Tarballs are mirrored to ~/Program/backups/xfce-m2-tarballs/ (target/ is
# volatile — see workspace AGENTS.md).
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/xfce_cross_env.sh"

XFCE_SRC="https://archive.xfce.org/xfce/4.20/src"

# Host tool: xdt-gen-visibility (from xfce4-dev-tools) is required
# unconditionally by libxfce4util's meson setup; xdt-csource is used by other
# Xfce components. Build the (arch-independent, script-only) dev tools for the
# host into $CROSSDIR/host-tools.
if ! command -v xdt-gen-visibility >/dev/null 2>&1; then
  echo "=== host xfce4-dev-tools: building ==="
  mkdir -p "$CROSSDIR/host-tools/src"
  fetch xfce4-dev-tools-4.20.0.tar.bz2 "$XFCE_SRC/xfce4-dev-tools-4.20.0.tar.bz2"
  if [ ! -d "$CROSSDIR/host-tools/src/xfce4-dev-tools-4.20.0" ]; then
    tar -C "$CROSSDIR/host-tools/src" -xf "$SRC/xfce4-dev-tools-4.20.0.tar.bz2"
  fi
  ( cd "$CROSSDIR/host-tools/src/xfce4-dev-tools-4.20.0" && \
    env -u CC -u CXX -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS \
    ./configure --prefix="$CROSSDIR/host-tools" && \
    make -j"$JOBS" && make install ) > "$LOGS/xfce4-dev-tools-host.log" 2>&1 \
    || { echo "FATAL: host xfce4-dev-tools build failed" >&2; exit 1; }
  export PATH="$CROSSDIR/host-tools/bin:$PATH"
fi

xfce_autotools() { # xfce_autotools <tarball> <url> [extra configure flags...]
  local tar="$1" url="$2"; shift 2
  cd "$SRC"
  local d; d=$(srcdir "$tar" "$url")
  cd "$SRC/$d"
  # shellcheck disable=SC2046
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    $(copt --disable-introspection) $(copt --disable-gtk-doc) \
    $(copt --disable-gtk-doc-html) $(copt --disable-gtk-doc-pdf) \
    $(copt --disable-debug) $(copt --disable-vala) \
    $(copt --disable-glade-catalog) $(copt --without-x) \
    "$@"
  make -j"$JOBS" && make install
}

build_libxfce4util() {
  # NOTE: the 4.20.0 release tarball's Meson build is incomplete (subdir
  # meson.build files were not dist'd — "Nonexistent build file
  # 'libxfce4util/meson.build'"), so this package builds with autotools.
  xfce_autotools libxfce4util-4.20.0.tar.bz2 "$XFCE_SRC/libxfce4util-4.20.0.tar.bz2"
}

build_xfconf() {
  # GDBus-based since 4.20 (no dbus-glib); gdbus-codegen is a host tool.
  xfce_autotools xfconf-4.20.0.tar.bz2 "$XFCE_SRC/xfconf-4.20.0.tar.bz2"
}

build_libxfce4ui() {
  # Hard deps: glib gtk3 libxfce4util libxfconf libX11 libICE libSM.
  # Optional (autodetected, we ship them): startup-notification, epoxy.
  # Optional (absent, autodetected off): libgtop, gudev, gladeui-2.0.
  xfce_autotools libxfce4ui-4.20.0.tar.bz2 "$XFCE_SRC/libxfce4ui-4.20.0.tar.bz2"
}

build_garcon() {
  xfce_autotools garcon-4.20.0.tar.bz2 "$XFCE_SRC/garcon-4.20.0.tar.bz2"
}

build_libwnck() {
  cd "$SRC"
  local d; d=$(srcdir libwnck-43.3.tar.xz \
    https://download.gnome.org/sources/libwnck/43/libwnck-43.3.tar.xz)
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" introspection disabled) \
    $(mopt "$SRC/$d/meson_options.txt" gtk_doc false) \
    $(mopt "$SRC/$d/meson_options.txt" startup_notification enabled) \
    $(mopt "$SRC/$d/meson_options.txt" install_tools false) \
    $(mopt "$SRC/$d/meson_options.txt" demos false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_exo() {
  xfce_autotools exo-4.20.0.tar.bz2 "$XFCE_SRC/exo-4.20.0.tar.bz2"
}

run_pkg libxfce4util build_libxfce4util
run_pkg xfconf       build_xfconf
run_pkg libxfce4ui   build_libxfce4ui
run_pkg garcon       build_garcon
run_pkg libwnck      build_libwnck
run_pkg exo          build_exo

echo "=== verify: XFCE-M2 artifacts in $PREFIX ==="
rc=0
for f in lib/libxfce4util.so.7 lib/libxfconf-0.so.3 lib/libxfce4ui-2.so.0 \
         lib/libgarcon-1.so.0 lib/libgarcon-gtk3-1.so.0 lib/libwnck-3.so.0 \
         lib/libexo-2.so.0 \
         lib/pkgconfig/libxfce4util-1.0.pc lib/pkgconfig/libxfconf-0.pc \
         lib/pkgconfig/libxfce4ui-2.pc lib/pkgconfig/garcon-1.pc \
         lib/pkgconfig/libwnck-3.0.pc lib/pkgconfig/exo-2.pc; do
  if [ -e "$PREFIX/$f" ]; then echo "OK   $f"; else echo "MISS $f"; rc=1; fi
done
exit "$rc"
