#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# XFCE-M2/M3 packaging: layer the cross-compiled Xfce libraries + desktop
# components (and their shared dependency closure) on top of a base systemd
# desktop initramfs.
#
# The full pipeline (tools/riscv/systemd/build_systemd_desktop.sh) cannot run
# right now because the systemd build tree under target/ was wiped together
# with the cross prefix. This overlay packer therefore starts from a base
# initramfs (default: ~/Program/backups/asterinas-desktop-20260820/, which
# predates XFCE-M1) and re-applies everything the Xfce chain needs:
#
#   * the M1 D-Bus system-bus payload (config + units) — the Aug-20 base
#     predates it, so the M1 unit/config steps are re-done here from the
#     versioned sources in tools/riscv/systemd/units/ and the M1-rebuilt
#     dbus-daemon in the cross prefix;
#   * the M2 shared-library payload (glib/GTK3/X11 + six Xfce libs);
#   * the M3 desktop payload (xfwm4/xfce4-panel/xfdesktop/xfce4-session/
#     xfsettingsd + Adwaita icons/cursors), and the unit swap that replaces
#     the matchbox desktop with xfce4-session.
#
# Unit-chain gotcha baked in: the base image carries default.target as a
# REGULAR FILE (old graphical.target copy), not a symlink — overriding
# graphical.target alone is inert. We therefore write the graphical.target
# content into default.target too.
#
# Output: target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/xfce_cross_env.sh"

BASE_INITRAMFS="${XFCE_BASE_INITRAMFS:-/home/arch-anjie/Program/backups/asterinas-desktop-20260820/systemd-desktop-initramfs.cpio}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
M1_UNITS="$ROOT/tools/riscv/systemd/units"
BUILD_ROOT="$ROOT/target/xfce-desktop"
ROOTFS="$BUILD_ROOT/rootfs"
OUTPUT="${1:-$ROOT/target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio}"

[[ -f "$BASE_INITRAMFS" ]] || { echo "missing base initramfs: $BASE_INITRAMFS" >&2; exit 2; }
[[ -f "$PREFIX/lib/libxfce4ui-2.so.0" ]] || { echo "missing Xfce libs in $PREFIX — run build_xfce_deps.sh + build_xfce_libs.sh first" >&2; exit 2; }

echo "=== extracting base initramfs ==="
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"
( cd "$ROOTFS" && cpio -id --quiet < "$BASE_INITRAMFS" )

echo "=== layering Xfce payload ==="

# 1. Shared libraries: the whole shared closure built by build_xfce_deps.sh +
#    build_xfce_libs.sh + build_xfce_apps.sh. Static-only artifacts
#    (libdbus-1.a, *.la, pkgconfig) stay behind. The guest's glibc loader
#    searches /usr/lib by default (proven by libxcvt.so.0 in the M1 image).
( cd "$PREFIX/lib" && find . -maxdepth 1 \( -name '*.so' -o -name '*.so.*' \) | cpio -pdm --quiet "$ROOTFS/usr/lib/" )

# 2. Executables: all of $PREFIX/bin is riscv64 userland from our scripts
#    (dbus tools, glib tools, the Xfce apps). lib/xfce4 holds xfconfd, the
#    panel plugins and the panel out-of-process wrapper; libexec has the
#    at-spi2 helpers + dbus-daemon-launch-helper.
cp -a "$PREFIX/bin/." "$ROOTFS/usr/bin/"
if [ -d "$PREFIX/lib/xfce4" ]; then
  mkdir -p "$ROOTFS/usr/lib/xfce4"
  cp -rL "$PREFIX/lib/xfce4/." "$ROOTFS/usr/lib/xfce4/"
fi
if [ -d "$PREFIX/libexec" ]; then
  mkdir -p "$ROOTFS/usr/libexec"
  cp -rL "$PREFIX/libexec/." "$ROOTFS/usr/libexec/"
fi

# 3. D-Bus payload (M1 re-applied onto the pre-M1 base):
#    etc/dbus-1 confs + share/dbus-1 (system/session bus config, activation
#    services), host-prefix paths rewritten to guest paths, runtime dirs.
mkdir -p "$ROOTFS/etc/dbus-1" "$ROOTFS/var/lib/dbus" "$ROOTFS/run/dbus"
if [ -d "$PREFIX/etc/dbus-1" ]; then
  cp -rL "$PREFIX/etc/dbus-1/." "$ROOTFS/etc/dbus-1/"
fi
if [ -d "$PREFIX/share/dbus-1" ]; then
  mkdir -p "$ROOTFS/usr/share/dbus-1"
  cp -rL "$PREFIX/share/dbus-1/." "$ROOTFS/usr/share/dbus-1/"
fi
for f in "$ROOTFS/etc/dbus-1/"*.conf "$ROOTFS/usr/share/dbus-1/"*.conf; do
  [ -f "$f" ] || continue
  sed -i -e "s|$PREFIX/etc/dbus-1|/etc/dbus-1|g" \
         -e "s|$PREFIX/var/run/dbus|/run/dbus|g" \
         -e "s|$PREFIX/libexec|/usr/libexec|g" \
         -e "s|$PREFIX/lib|/usr/lib|g" \
         -e "s|$PREFIX/bin|/usr/bin|g" \
         -e "s|$PREFIX|/usr|g" \
         -e '/^[[:space:]]*<fork\/>/d' \
         -e '/^[[:space:]]*<syslog\/>/d' \
         -e '/^[[:space:]]*<pidfile>/d' "$f"
done
find "$ROOTFS/usr/share/dbus-1/services" "$ROOTFS/usr/share/dbus-1/accessibility-services" \
  -name '*.service' -exec sed -i \
    -e "s|$PREFIX/libexec|/usr/libexec|g" \
    -e "s|$PREFIX/lib|/usr/lib|g" \
    -e "s|$PREFIX/bin|/usr/bin|g" {} + 2>/dev/null || true

# 3b. Manager DefaultEnvironment: base image (pre-M1) lacks
#     DBUS_SYSTEM_BUS_ADDRESS — the dbus clients' compiled-in default socket
#     path is the *host* prefix's var/run/dbus.
if ! grep -q DBUS_SYSTEM_BUS_ADDRESS "$ROOTFS/etc/systemd/system.conf" 2>/dev/null; then
  cat > "$ROOTFS/etc/systemd/system.conf" <<'EOF'
[Manager]
DefaultEnvironment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
EOF
fi

# 4. Unit set: M1 units from the repo, then the M3 Xfce overrides, then drop
#    the matchbox-era desktop services. default.target is a regular file in
#    the base image (see header), so mirror graphical.target into it.
cp "$M1_UNITS"/*.service "$M1_UNITS"/*.target "$ROOTFS/etc/systemd/system/"
cp "$SRC_DIR"/units/*.service "$SRC_DIR"/units/*.target "$ROOTFS/etc/systemd/system/"
rm -f "$ROOTFS/etc/systemd/system/"{matchbox-window-manager,xpanel,pcmanfm,netsurf,curl-cert-test}.service
cp "$ROOTFS/etc/systemd/system/graphical.target" "$ROOTFS/etc/systemd/system/default.target"

# 5. Session entry point.
cp "$SRC_DIR/xfce-session-start" "$ROOTFS/usr/bin/xfce-session-start"
chmod +x "$ROOTFS/usr/bin/xfce-session-start"

# 6. Data: package data dirs, icons (hicolor + Adwaita incl. cursors),
#    pixmaps, .desktop files, shared-mime-info db, gsettings schemas
#    (gschemas.compiled generated with the HOST tool — arch-independent).
for d in exo garcon xfce4 xfconf xfwm4 xfdesktop themes; do
  if [ -d "$PREFIX/share/$d" ]; then
    mkdir -p "$ROOTFS/usr/share/$d"
    cp -rL "$PREFIX/share/$d/." "$ROOTFS/usr/share/$d/"
  fi
done
for theme in hicolor Adwaita; do
  if [ -d "$PREFIX/share/icons/$theme" ]; then
    mkdir -p "$ROOTFS/usr/share/icons/$theme"
    cp -rL "$PREFIX/share/icons/$theme/." "$ROOTFS/usr/share/icons/$theme/"
  fi
done
if [ -d "$PREFIX/share/pixmaps" ]; then
  mkdir -p "$ROOTFS/usr/share/pixmaps"
  cp -rL "$PREFIX/share/pixmaps/." "$ROOTFS/usr/share/pixmaps/"
fi
if [ -d "$PREFIX/share/applications" ]; then
  mkdir -p "$ROOTFS/usr/share/applications"
  cp "$PREFIX/share/applications/"*.desktop "$ROOTFS/usr/share/applications/" 2>/dev/null || true
fi
if [ -d "$PREFIX/share/mime" ]; then
  mkdir -p "$ROOTFS/usr/share/mime"
  cp -rL "$PREFIX/share/mime/." "$ROOTFS/usr/share/mime/"
fi
if [ -d "$PREFIX/share/glib-2.0/schemas" ]; then
  mkdir -p "$ROOTFS/usr/share/glib-2.0/schemas"
  cp "$PREFIX/share/glib-2.0/schemas/"*.gschema.xml "$ROOTFS/usr/share/glib-2.0/schemas/" 2>/dev/null || true
  glib-compile-schemas "$ROOTFS/usr/share/glib-2.0/schemas" || true
fi
# XDG autostart (xfsettingsd, at-spi2) + default xfconf channel/panel layout.
if [ -d "$PREFIX/etc/xdg" ]; then
  mkdir -p "$ROOTFS/etc/xdg"
  cp -rL "$PREFIX/etc/xdg/." "$ROOTFS/etc/xdg/"
fi
# xfconf system defaults for this guest: compositor off (fbdev Xorg has no
# GLX/Present; xfwm4 stalls right after its XRes probe otherwise — observed
# in the M3 serial log), and a failsafe session without Thunar (not built).
mkdir -p "$ROOTFS/etc/xdg/xfce4/xfconf/xfce-perchannel-xml"
cp "$SRC_DIR"/xfconf-defaults/*.xml "$ROOTFS/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/"
# Some components bake $PREFIX/etc/... (libxfce4ui's autostart lookup reads
# $PREFIX/etc/xdg/autostart/xfsettingsd.desktop); the M1 host-prefix symlink
# bridge maps that to /usr/etc/..., so bridge it to /etc.
mkdir -p "$ROOTFS/usr/etc"
ln -sfn /etc/xdg "$ROOTFS/usr/etc/xdg"
# at-spi2 bus config (its baked path resolves through the M1 host-prefix
# symlink bridge /home/.../riscv-cross/usr -> /usr).
if [ -d "$PREFIX/share/defaults" ]; then
  mkdir -p "$ROOTFS/usr/share/defaults"
  cp -rL "$PREFIX/share/defaults/." "$ROOTFS/usr/share/defaults/"
fi
# pnp.ids for libdisplay-info (xfce4-display-settings / xfsettingsd EDID).
if [ -f /usr/share/hwdata/pnp.ids ]; then
  mkdir -p "$ROOTFS/usr/share/hwdata"
  cp /usr/share/hwdata/pnp.ids "$ROOTFS/usr/share/hwdata/pnp.ids"
fi
# Demo images should never blank the screen mid-run (Xorg's default DPMS /
# screensaver blanked the M3 verification shot): append a ServerFlags stanza
# to the staged xorg.conf.
if [ -f "$ROOTFS/etc/xorg.conf" ] && ! grep -q "XFCE-M3" "$ROOTFS/etc/xorg.conf"; then
  {
    echo ""
    echo "# XFCE-M3: never blank/screensave in the demo guest."
    echo 'Section "ServerFlags"'
    echo '    Option "BlankTime" "0"'
    echo '    Option "StandbyTime" "0"'
    echo '    Option "SuspendTime" "0"'
    echo '    Option "OffTime" "0"'
    echo "EndSection"
  } >> "$ROOTFS/etc/xorg.conf"
fi
# glibc gconv modules: the guest glibc runtime ships without them, so glib's
# iconv-based charset conversion fails (observed: Gdk "Conversion from
# ISO-8859-1 to UTF-8 is not supported" from xfwm4/xfce4-panel). They come
# from the cross toolchain sysroot (riscv64 glibc, matches /lib/libc.so.6).
if [ -d /usr/riscv64-linux-gnu/usr/lib/gconv ]; then
  mkdir -p "$ROOTFS/usr/lib/gconv"
  cp -rL /usr/riscv64-linux-gnu/usr/lib/gconv/. "$ROOTFS/usr/lib/gconv/"
fi

# 7. Strip freshly copied ELF payload (M1 kept binaries stripped to stay under
#    the initrd size ceiling).
find "$ROOTFS/usr/lib" "$ROOTFS/usr/libexec" -name '*.so*' -type f -exec "$STRIP" --strip-unneeded {} + 2>/dev/null || true
for b in "$ROOTFS"/usr/bin/* "$ROOTFS"/usr/lib/xfce4/xfconf/xfconfd \
         "$ROOTFS"/usr/lib/xfce4/panel/wrapper-2.0 "$ROOTFS"/usr/libexec/*; do
  if [ -f "$b" ] && file "$b" | grep -q ELF; then
    "$STRIP" --strip-unneeded "$b" 2>/dev/null || true
  fi
done

echo "=== packing ==="
mkdir -p "$(dirname "$OUTPUT")"
( cd "$ROOTFS" && find . | cpio -o -H newc --quiet > "$OUTPUT" )

echo "built $OUTPUT"
echo "  rootfs:    $(du -sh "$ROOTFS" | cut -f1)"
echo "  initramfs: $(du -h "$OUTPUT" | cut -f1)"
echo "--- Xfce payload in image ---"
rc=0
for f in usr/lib/libxfce4util.so.7 usr/lib/libxfconf-0.so.3 usr/lib/libxfce4ui-2.so.0 \
         usr/lib/libgarcon-1.so.0 usr/lib/libgarcon-gtk3-1.so.0 usr/lib/libwnck-3.so.0 \
         usr/lib/libexo-2.so.0 usr/lib/libxfce4windowing-0.so.0 \
         usr/bin/xfwm4 usr/bin/xfce4-panel usr/bin/xfdesktop usr/bin/xfce4-session \
         usr/bin/xfsettingsd usr/bin/xfce-session-start \
         usr/lib/xfce4/xfconf/xfconfd usr/bin/dbus-daemon usr/bin/dbus-run-session \
         etc/dbus-1/system.conf etc/systemd/system/xfce-session.service \
         usr/share/icons/Adwaita/index.theme usr/share/icons/Adwaita/cursors/left_ptr; do
  if [ -e "$ROOTFS/$f" ]; then echo "OK   $f"; else echo "MISS $f"; rc=1; fi
done
exit "$rc"
