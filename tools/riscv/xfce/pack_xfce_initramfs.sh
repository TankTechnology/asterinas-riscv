#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# XFCE-M2 packaging: layer the cross-compiled Xfce core libraries (and their
# shared dependency closure) on top of the proven systemd desktop initramfs.
#
# The full pipeline (tools/riscv/systemd/build_systemd_desktop.sh) cannot run
# right now because the systemd build tree under target/ was wiped together
# with the cross prefix. This overlay packer therefore starts from the backup
# initramfs (~/Program/backups/asterinas-desktop-20260820/, a byte-identical
# copy of what build_systemd_desktop.sh produced at M1), extracts it, adds the
# Xfce payload from target/riscv-cross/usr, and repacks a raw newc cpio.
# Once the systemd build tree is restored, the same payload is picked up by
# step 8d of build_systemd_desktop.sh and this script becomes redundant.
#
# Output: target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/xfce_cross_env.sh"

BASE_INITRAMFS="${XFCE_BASE_INITRAMFS:-/home/arch-anjie/Program/backups/asterinas-desktop-20260820/systemd-desktop-initramfs.cpio}"
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
#    build_xfce_libs.sh (glib/gio, GTK3 stack, X11 client libs, the six Xfce
#    libs). Static-only artifacts (libdbus-1.a, *.la, pkgconfig) stay behind.
#    The guest's glibc loader searches /usr/lib by default (proven by
#    libxcvt.so.0 resolving there in the M1 desktop image).
( cd "$PREFIX/lib" && find . -maxdepth 1 \( -name '*.so' -o -name '*.so.*' \) | cpio -pdm --quiet "$ROOTFS/usr/lib/" )

# 2. Executables installed by the Xfce packages (xfconfd is the settings
#    daemon backing libxfconf; the exo/libxfce4ui helpers are useful for M3
#    smoke tests). xfconfd installs to lib/xfce4/xfconf/ (D-Bus activated);
#    the at-spi2 helpers in libexec/ back GTK3's atk-bridge.
for tool in xfconfd xfce4-about xfce4-keyboard-shortcuts \
            exo-open exo-desktop-item-edit exo-preferred-applications; do
  if [ -f "$PREFIX/bin/$tool" ]; then
    cp "$PREFIX/bin/$tool" "$ROOTFS/usr/bin/$tool"
  fi
done
if [ -d "$PREFIX/lib/xfce4" ]; then
  mkdir -p "$ROOTFS/usr/lib/xfce4"
  cp -rL "$PREFIX/lib/xfce4/." "$ROOTFS/usr/lib/xfce4/"
fi
for helper in at-spi-bus-launcher at-spi2-registryd; do
  if [ -f "$PREFIX/libexec/$helper" ]; then
    mkdir -p "$ROOTFS/usr/libexec"
    cp "$PREFIX/libexec/$helper" "$ROOTFS/usr/libexec/$helper"
  fi
done

# 3. D-Bus service activation files (xfconf's org.xfce.Xfconf.service, plus
#    the at-spi2 accessibility bus entries that GTK3's atk-bridge activates).
for svcdir in services accessibility-services; do
  if [ -d "$PREFIX/share/dbus-1/$svcdir" ]; then
    mkdir -p "$ROOTFS/usr/share/dbus-1/$svcdir"
    cp "$PREFIX/share/dbus-1/$svcdir/"*.service "$ROOTFS/usr/share/dbus-1/$svcdir/"
  fi
done
# Baked host-prefix Exec= paths -> canonical guest locations (same rewrite as
# the dbus step in build_systemd_desktop.sh).
find "$ROOTFS/usr/share/dbus-1/services" "$ROOTFS/usr/share/dbus-1/accessibility-services" \
  -name '*.service' -exec sed -i \
    -e "s|$PREFIX/libexec|/usr/libexec|g" \
    -e "s|$PREFIX/lib|/usr/lib|g" \
    -e "s|$PREFIX/bin|/usr/bin|g" {} + 2>/dev/null || true

# 4. Data installed by the six packages: libxfce4ui icons/pixmaps, exo
#    data + preferred-apps .desktop files, garcon menu data, xfconf helpers.
for d in exo garcon xfce4 xfconf; do
  if [ -d "$PREFIX/share/$d" ]; then
    mkdir -p "$ROOTFS/usr/share/$d"
    cp -rL "$PREFIX/share/$d/." "$ROOTFS/usr/share/$d/"
  fi
done
# Icon/theme assets installed by libxfce4ui + exo into hicolor/pixmaps.
if [ -d "$PREFIX/share/icons/hicolor" ]; then
  mkdir -p "$ROOTFS/usr/share/icons/hicolor"
  cp -rL "$PREFIX/share/icons/hicolor/." "$ROOTFS/usr/share/icons/hicolor/"
fi
if [ -d "$PREFIX/share/pixmaps" ]; then
  mkdir -p "$ROOTFS/usr/share/pixmaps"
  cp -rL "$PREFIX/share/pixmaps/." "$ROOTFS/usr/share/pixmaps/"
fi
# exo's .desktop entries for preferred applications.
if [ -d "$PREFIX/share/applications" ]; then
  mkdir -p "$ROOTFS/usr/share/applications"
  cp "$PREFIX/share/applications/"*.desktop "$ROOTFS/usr/share/applications/" 2>/dev/null || true
fi
# freedesktop.org shared-mime-info database (garcon/glib MIME lookup) and the
# GLib gsettings schemas shipped by gtk3/libxfce4ui. gschemas.compiled is
# generated at pack time with the HOST glib-compile-schemas (output is
# arch-independent); the guest cannot run the cross-built one at first boot.
if [ -d "$PREFIX/share/mime" ]; then
  mkdir -p "$ROOTFS/usr/share/mime"
  cp -rL "$PREFIX/share/mime/." "$ROOTFS/usr/share/mime/"
fi
if [ -d "$PREFIX/share/glib-2.0/schemas" ]; then
  mkdir -p "$ROOTFS/usr/share/glib-2.0/schemas"
  cp "$PREFIX/share/glib-2.0/schemas/"*.gschema.xml "$ROOTFS/usr/share/glib-2.0/schemas/" 2>/dev/null || true
  glib-compile-schemas "$ROOTFS/usr/share/glib-2.0/schemas" || true
fi

# 5. Strip the freshly copied shared libs and helper daemons (the M1 desktop
#    kept its binaries stripped to stay under the initrd size ceiling).
find "$ROOTFS/usr/lib" "$ROOTFS/usr/libexec" -name '*.so.*' -type f -exec "$STRIP" --strip-unneeded {} + 2>/dev/null || true
for helper in "$ROOTFS/usr/lib/xfce4/xfconf/xfconfd" "$ROOTFS/usr/libexec/at-spi-bus-launcher" \
              "$ROOTFS/usr/libexec/at-spi2-registryd"; do
  [ -f "$helper" ] && "$STRIP" --strip-unneeded "$helper" 2>/dev/null || true
done

echo "=== packing ==="
mkdir -p "$(dirname "$OUTPUT")"
( cd "$ROOTFS" && find . | cpio -o -H newc --quiet > "$OUTPUT" )

echo "built $OUTPUT"
echo "  rootfs:    $(du -sh "$ROOTFS" | cut -f1)"
echo "  initramfs: $(du -h "$OUTPUT" | cut -f1)"
echo "--- Xfce libs in image ---"
for f in libxfce4util.so.7 libxfconf-0.so.3 libxfce4ui-2.so.0 \
         libgarcon-1.so.0 libgarcon-gtk3-1.so.0 libwnck-3.so.0 libexo-2.so.0; do
  if [ -e "$ROOTFS/usr/lib/$f" ]; then echo "OK   usr/lib/$f"; else echo "MISS usr/lib/$f"; fi
done
