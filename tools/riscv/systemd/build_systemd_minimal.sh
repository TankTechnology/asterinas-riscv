#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Attempt a MINIMAL cross-compile of systemd for riscv64 glibc: pid1 + journald
# + the core management tools (systemctl/journalctl), with almost every
# optional subsystem disabled. This is a feasibility probe — the goal is to
# record exactly where a cross build breaks, not to produce a shippable init.
#
# Option names are validated against meson_options.txt @ v257.5.
# Required already in target/riscv-cross/usr: libcap (built), libmount/libuuid
# (util-linux, built), libz. Host needs meson/ninja/python3-jinja2.
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VERSION=257.5
JOBS="$(nproc)"

export PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"

SRCDIR="$SRC/systemd-$VERSION"
[ -d "$SRCDIR" ] || { echo "missing $SRCDIR (extract the tarball first)"; exit 1; }

CROSS="$SRC/cross-systemd-riscv64.txt"
cat > "$CROSS" <<EOF
[binaries]
c = '${HOST}-gcc'
cpp = '${HOST}-g++'
ar = '${HOST}-ar'
strip = '${HOST}-strip'
# Static resolution: meson does NOT pull Requires.private/Libs.private even
# with default_library=static, so route pkg-config through the --static wrapper
# (mount.pc -> Requires.private: blkid).
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

cd "$SRCDIR"
rm -rf build-riscv
meson setup build-riscv --cross-file "$CROSS" --prefix="$PREFIX" \
  --default-library=static -Dmode=release \
  `# no .git in a GitHub archive tarball -> skip git describe` \
  -Dvcs-tag=false \
  `# privilege / MAC / crypto stack — all off for a bare pid1+journald` \
  -Dseccomp=false -Dselinux=false -Dapparmor=false -Dsmack=false \
  -Dpolkit=false -Dima=false -Dipe=false -Dxenctrl=false \
  -Dpam=false -Daudit=false -Dacl=false -Dkmod=false \
  -Dgnutls=false -Dopenssl=false -Dgcrypt=false -Dlibfido2=false \
  -Dlibcryptsetup=false -Dlibcryptsetup-plugins=false \
  -Dp11kit=false -Dtpm2=false -Ddefault-dnssec=no \
  `# compression: zlib only (present); drop zstd/xz/lz4/bzip2` \
  -Dzlib=true -Dxz=false -Dlz4=false -Dzstd=false -Dbzip2=false \
  `# network / http / idn / dbus / glib / pcre2` \
  -Dlibcurl=false -Dlibidn=false -Dlibidn2=false -Dmicrohttpd=false \
  -Dqrencode=false -Ddbus=false -Dglib=false -Dpcre2=false \
  -Dpasswdqc=false -Dpwquality=false -Dlibarchive=disabled \
  `# whole subsystems that pull foreign deps or generate build-time data` \
  -Dimportd=false -Dremote=false -Dnetworkd=false -Dresolve=false \
  -Dtimesyncd=false -Dhomed=false -Duserdb=false -Dcoredump=false \
  -Dfirstboot=false -Dhibernate=false -Dlogind=false -Dnss-systemd=false \
  -Dnss-myhostname=false -Dnss-mymachines=false -Dnss-resolve=false \
  -Delfutils=false -Dbpf-framework=false -Dldconfig=false \
  -Dhwdb=false -Dsysupdate=false -Dsysupdated=disabled -Drepart=false \
  -Dfdisk=false -Dbinfmt=false -Dvconsole=false \
  -Dquotacheck=false -Dtmpfiles=false -Drandomseed=false -Dsysext=false \
  -Dportabled=false -Dutmp=false -Denvironment-d=false \
  -Dhostnamed=false -Dlocaled=false -Dmachined=false -Dtimedated=false \
  -Dsysusers=false -Danalyze=false -Dbacklight=false -Drfkill=false \
  -Dpstore=false -Doomd=false -Dmountfsd=false -Dnsresourced=false \
  -Dvmspawn=false -Dstoragetm=false -Dxdg-autostart=false -Dkernel-install=false \
  -Dbootloader=false -Defi=false -Dfexecve=false -Dxkbcommon=false \
  -Dlibiptc=false -Dukify=false -Dtests=false -Dinstall-tests=false \
  -Dman=false -Dhtml=false -Dtranslations=false

ninja -C build-riscv -j"$JOBS"
echo "systemd minimal cross-build SUCCEEDED (see build-riscv)"
