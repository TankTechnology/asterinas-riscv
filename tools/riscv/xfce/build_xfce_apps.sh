#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# XFCE-M3: cross-compile the Xfce desktop *components* for riscv64 on top of
# the M2 library matrix (build_xfce_deps.sh + build_xfce_libs.sh):
#
#   deps:      libXinerama libXpresent libyaml libdisplay-info
#   libs:      libxfce4windowing        (xfce4-panel 4.20's windowing backend)
#   desktop:   xfwm4 xfce4-panel xfdesktop xfce4-session xfce4-settings
#   data:      adwaita-icon-theme (icons + Adwaita Xcursor theme)
#
# Notable version facts discovered while wiring this up:
#   * libxfce4windowing 4.20's X11 backend hard-requires libdisplay-info.
#   * xfdesktop 4.20 requires libyaml (settings are YAML).
#   * xfwm4 4.20 requires libXinerama (non-optional XDT_CHECK_PACKAGE).
#   * xfce4-session 4.20 probes iceauth with AC_PATH_PROG and tolerates its
#     absence (no ICEAUTH_CMD then); no need to build x11-apps.
#   * xfce4-settings' libxklavier/colord/upower/libnotify are optional; only
#     XRANDR/XCURSOR (present in the prefix) get picked up.
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/xfce_cross_env.sh"

XFCE_SRC="https://archive.xfce.org/xfce/4.20/src"

# ------------------------------------------------------------------ deps ----

build_libXinerama() {
  cd "$SRC"
  local d; d=$(srcdir libXinerama-1.1.5.tar.xz \
    https://www.x.org/releases/individual/lib/libXinerama-1.1.5.tar.xz)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" --enable-shared --disable-static \
    $(copt --disable-malloc0returnsnull)
  make -j"$JOBS" && make install
}

build_libXpresent() {
  cd "$SRC"
  local d; d=$(srcdir libXpresent-1.0.1.tar.xz \
    https://www.x.org/releases/individual/lib/libXpresent-1.0.1.tar.xz)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" --enable-shared --disable-static \
    $(copt --disable-malloc0returnsnull)
  make -j"$JOBS" && make install
}

build_libyaml() {
  cd "$SRC"
  local d; d=$(srcdir yaml-0.2.5.tar.gz \
    https://github.com/yaml/libyaml/releases/download/0.2.5/yaml-0.2.5.tar.gz)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" --enable-shared --disable-static
  make -j"$JOBS" && make install
}

build_libdisplay_info() {
  cd "$SRC"
  local d; d=$(srcdir libdisplay-info-0.2.0.tar.gz \
    https://gitlab.freedesktop.org/emersion/libdisplay-info/-/archive/0.2.0/libdisplay-info-0.2.0.tar.gz)
  # hwdata (host) is used only to bake the pnp.ids path; the file is data and
  # shipped in the guest via pack_xfce_initramfs.sh.
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" tests false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

# ------------------------------------------------------------------ libs ----

build_libxfce4windowing() {
  cd "$SRC"
  local d; d=$(srcdir libxfce4windowing-4.20.0.tar.bz2 "$XFCE_SRC/libxfce4windowing-4.20.0.tar.bz2")
  # X11 backend only (wayland needs wayland-client + wlr protocols).
  # shellcheck disable=SC2046
  MESON_SETUP "$SRC/$d" \
    $(mopt "$SRC/$d/meson_options.txt" x11 enabled) \
    $(mopt "$SRC/$d/meson_options.txt" wayland disabled) \
    $(mopt "$SRC/$d/meson_options.txt" gtk-doc false) \
    $(mopt "$SRC/$d/meson_options.txt" introspection false) \
    $(mopt "$SRC/$d/meson_options.txt" visibility false) \
    $(mopt "$SRC/$d/meson_options.txt" tests false)
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

# --------------------------------------------------------------- desktop ----

xfce_app() { # xfce_app <tarball> [extra configure flags...]
  local tar="$1"; shift
  cd "$SRC"
  local d; d=$(srcdir "$tar" "$XFCE_SRC/$tar")
  cd "$SRC/$d"
  # shellcheck disable=SC2046
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    $(copt --disable-introspection) $(copt --disable-gtk-doc) \
    $(copt --disable-gtk-doc-html) $(copt --disable-gtk-doc-pdf) \
    $(copt --disable-debug) $(copt --disable-vala) \
    $(copt --disable-gtk-layer-shell) $(copt --disable-dbusmenu) \
    $(copt --disable-polkit) $(copt --disable-colord) $(copt --disable-upower) \
    $(copt --disable-libnotify) $(copt --disable-libxklavier) \
    $(copt --disable-libinput) $(copt --disable-wayland) \
    "$@"
  make -j"$JOBS" && make install
}

build_xfwm4()          { xfce_app xfwm4-4.20.0.tar.bz2; }
build_xfce4_panel()    { xfce_app xfce4-panel-4.20.0.tar.bz2; }
build_xfdesktop()      { xfce_app xfdesktop-4.20.0.tar.bz2; }

build_iceauth() {
  # xfce4-session's configure hard-requires iceauth (bakes ICEAUTH_CMD for the
  # guest's ICE authority management at runtime) — so cross-build the target
  # binary and point configure at it.
  cd "$SRC"
  local d; d=$(srcdir iceauth-1.0.10.tar.xz \
    https://www.x.org/releases/individual/app/iceauth-1.0.10.tar.xz)
  cd "$SRC/$d"
  ./configure --host="$HOST" --prefix="$PREFIX" --enable-shared --disable-static
  make -j"$JOBS" && make install
}

build_xfce4_session() {
  cd "$SRC"
  local d; d=$(srcdir xfce4-session-4.20.0.tar.bz2 "$XFCE_SRC/xfce4-session-4.20.0.tar.bz2")
  cd "$SRC/$d"
  # shellcheck disable=SC2046
  ./configure --host="$HOST" --prefix="$PREFIX" \
    --enable-shared --disable-static \
    ICEAUTH="$PREFIX/bin/iceauth" \
    --with-xsession-prefix="$PREFIX" \
    --with-helper-path-prefix="$PREFIX" \
    $(copt --disable-introspection) $(copt --disable-gtk-doc) \
    $(copt --disable-gtk-doc-html) $(copt --disable-gtk-doc-pdf) \
    $(copt --disable-debug) $(copt --disable-vala) \
    $(copt --disable-gtk-layer-shell) $(copt --disable-polkit) \
    $(copt --disable-wayland)
  make -j"$JOBS" && make install
}
build_xfce4_settings() { xfce_app xfce4-settings-4.20.0.tar.bz2; }

# ------------------------------------------------------------------ data ----

build_hicolor_icons() {
  cd "$SRC"
  local d; d=$(srcdir hicolor-icon-theme-0.18.tar.xz \
    https://icon-theme.freedesktop.org/releases/hicolor-icon-theme-0.18.tar.xz)
  # Provides hicolor/index.theme — the mandatory fallback theme every icon
  # lookup ends in (gtk warns "The 'hicolor' theme was not found" without it).
  MESON_SETUP "$SRC/$d"
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

build_adwaita_icons() {
  cd "$SRC"
  local d; d=$(srcdir adwaita-icon-theme-47.0.tar.xz \
    https://download.gnome.org/sources/adwaita-icon-theme/47/adwaita-icon-theme-47.0.tar.xz)
  # Data-only for us: the tarball ships pre-rendered icons AND the Adwaita
  # Xcursor theme. Meson install just copies files.
  MESON_SETUP "$SRC/$d"
  NINJA_INSTALL "$SRC/$d/build-riscv"
}

# ------------------------------------------------------------------ main ----

run_pkg libXinerama         build_libXinerama
run_pkg libXpresent         build_libXpresent
run_pkg libyaml             build_libyaml
run_pkg libdisplay-info     build_libdisplay_info
run_pkg libxfce4windowing   build_libxfce4windowing
run_pkg xfwm4               build_xfwm4
run_pkg xfce4-panel         build_xfce4_panel
run_pkg xfdesktop           build_xfdesktop
run_pkg iceauth             build_iceauth
run_pkg xfce4-session       build_xfce4_session
run_pkg xfce4-settings      build_xfce4_settings
run_pkg hicolor-icon-theme  build_hicolor_icons
run_pkg adwaita-icon-theme  build_adwaita_icons

echo "=== verify: XFCE-M3 artifacts in $PREFIX ==="
rc=0
for f in bin/xfwm4 bin/xfce4-panel bin/xfdesktop bin/xfce4-session \
         bin/xfsettingsd bin/xfce4-session-logout \
         lib/libxfce4windowing-0.so.0 lib/libxfce4windowingui-0.so.0 \
         lib/libXinerama.so.1 lib/libXpresent.so.1 lib/libyaml-0.so.2 \
         lib/libdisplay-info.so.2 \
         share/icons/Adwaita/index.theme share/icons/Adwaita/cursors/left_ptr \
         share/icons/hicolor/index.theme; do
  if [ -e "$PREFIX/$f" ]; then echo "OK   $f"; else echo "MISS $f"; rc=1; fi
done
exit "$rc"
