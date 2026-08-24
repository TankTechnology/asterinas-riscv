#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-compile the classic D-Bus reference daemon (dbus-daemon) + libdbus-1.a
# for riscv64 glibc into target/riscv-cross/usr.
#
# systemd's runtime depends on a D-Bus system bus (sd-bus talks to it for
# logind/hostnamed/etc.; here those subsystems are trimmed off, but a system
# bus + dbus-daemon are still part of the "runtime ecosystem" this milestone
# assembles). We build the REFERENCE implementation (dbus-daemon), not
# dbus-broker: dbus-broker is a meson project that requires a working systemd
# (sd-bus + libsystemd shared lib), which is exactly the dynamic-linking blocker
# M2 documented — the classic daemon is a self-contained C program.
#
# NOTE: dbus >= 1.16 is Meson-only (autotools was dropped), so this uses the
# same --cross-file + pkg-config-static pattern as build_systemd_minimal.sh.
# Static by default so dbus-daemon is usable without the dynamic linker that the
# systemd pid1 build still depends on. libexpat is required (already present in
# the prefix from the desktop X build).
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VERSION=1.16.2
JOBS="$(nproc)"

export PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"

mkdir -p "$SRC"
cd "$SRC"

tarball="dbus-$VERSION.tar.xz"
dir="dbus-$VERSION"
BASE=https://dbus.freedesktop.org/releases/dbus

if [ ! -d "$dir" ]; then
  if [ ! -f "$tarball" ]; then
    echo "  downloading $tarball"
    curl -fsSL --retry 6 --retry-all-errors --retry-delay 2 -o "$tarball" "$BASE/$tarball"
  fi
  echo "  extracting"
  tar xf "$tarball"
fi

CROSS="$SRC/cross-dbus-riscv64.txt"
cat > "$CROSS" <<EOF
[binaries]
c = '${HOST}-gcc'
cpp = '${HOST}-g++'
ar = '${HOST}-ar'
strip = '${HOST}-strip'
pkgconfig = '${ROOT}/target/riscv-cross/pkg-config-static'

[host_machine]
system = 'linux'
cpu_family = 'riscv64'
cpu = 'riscv64'
endian = 'little'

[properties]
pkg_config_libdir = '${PREFIX}/lib/pkgconfig:${PREFIX}/share/pkgconfig'

[built-in options]
c_args = ['-I${PREFIX}/include']
c_link_args = ['-L${PREFIX}/lib']
cpp_args = ['-I${PREFIX}/include']
cpp_link_args = ['-L${PREFIX}/lib']
EOF

cd "$dir"
rm -rf build-riscv
meson setup build-riscv --cross-file "$CROSS" --prefix="$PREFIX" \
  --default-library=static -Dbuildtype=release \
  `# keep dbus-daemon + CLI tools; drop the foreign/security backends` \
  -Dmessage_bus=true -Dtools=true -Dtraditional_activation=true \
  `# MAC/audit/systemd/X11 backends — all off (systemd off = no libsystemd)` \
  -Dapparmor=disabled -Dselinux=disabled -Dlibaudit=disabled \
  -Dsystemd=disabled -Dx11_autolaunch=disabled \
  `# tests / docs / assertions` \
  -Dmodular_tests=disabled -Dxml_docs=disabled -Ddoxygen_docs=disabled \
  -Dducktype_docs=disabled -Dasserts=false -Dverbose_mode=false \
  `# run the system bus as root for a minimal single-user rootfs` \
  -Duser_session=false -Ddbus_user=root

ninja -C build-riscv -j"$JOBS"
# Install straight into the cross prefix (same convention as the other
# build_*.sh scripts: --prefix=$PREFIX, no DESTDIR). The post-install script
# only rewrites the .pc prefix (relocation is off) and chown/chmods the
# dbus-daemon-launch-helper setuid — it never runs a target binary, so it is
# safe under cross-compilation.
ninja -C build-riscv install

echo "=== verify ==="
rc=0
for f in bin/dbus-daemon bin/dbus-send bin/dbus-monitor bin/dbus-uuidgen \
         lib/libdbus-1.a etc/dbus-1/system.conf etc/dbus-1/session.conf; do
  if [ -e "$PREFIX/$f" ]; then echo "OK   $f"; else echo "MISS $f"; rc=1; fi
done
echo "--- dbus-daemon linkage ---"
file "$PREFIX/bin/dbus-daemon" 2>/dev/null || true
readelf -d "$PREFIX/bin/dbus-daemon" 2>/dev/null | grep -E 'NEEDED|interpreter' || true
exit "$rc"
