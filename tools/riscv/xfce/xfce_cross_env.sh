# SPDX-License-Identifier: MPL-2.0
# Common cross-compile environment for the XFCE-M2 dependency matrix
# (libxfce4util -> xfconf -> libxfce4ui -> garcon -> libwnck -> exo).
#
# Sourced by build_xfce_deps.sh / build_xfce_libs.sh. Same conventions as
# tools/riscv/systemd/build_dbus.sh and tools/riscv/xorg/build_*.sh:
# everything lands in target/riscv-cross/usr, sources in target/riscv-cross/src,
# per-package logs in target/riscv-cross/logs, and a marker file per finished
# package so re-runs are idempotent.
#
# Downloaded tarballs are ALSO mirrored to ~/Program/backups/xfce-m2-tarballs/
# because target/ is a volatile area (see workspace AGENTS.md note of
# 2026-08-20: target/ was wiped on 2026-08-19 and cost 6+ hours to rebuild).

ROOT=/home/arch-anjie/Program/asterinas-riscv
CROSSDIR="$ROOT/target/riscv-cross"
PREFIX="$CROSSDIR/usr"
SRC="$CROSSDIR/src"
LOGS="$CROSSDIR/logs"
HOST=riscv64-linux-gnu
JOBS="$(nproc)"
MIRROR=/home/arch-anjie/Program/backups/xfce-m2-tarballs

export CC="$HOST-gcc"
export CXX="$HOST-g++"
export AR="$HOST-ar"
export STRIP="$HOST-strip"
export RANLIB="$HOST-ranlib"

# pkg-config wrappers. The historical `pkg-config-static` wrapper (used by the
# systemd/dbus/xorg scripts) forces --static so meson/autotools resolve
# Requires.private closures; `pkg-config-cross` is the plain variant for the
# shared-library builds done here.
mkdir -p "$CROSSDIR" "$SRC" "$LOGS" "$MIRROR"
if [ ! -x "$CROSSDIR/pkg-config-static" ]; then
  printf '#!/bin/sh\nexec pkg-config --static "$@"\n' > "$CROSSDIR/pkg-config-static"
  chmod +x "$CROSSDIR/pkg-config-static"
fi
if [ ! -x "$CROSSDIR/pkg-config-cross" ]; then
  printf '#!/bin/sh\nexec pkg-config "$@"\n' > "$CROSSDIR/pkg-config-cross"
  chmod +x "$CROSSDIR/pkg-config-cross"
fi

export PKG_CONFIG="$CROSSDIR/pkg-config-cross"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
# NOTE: deliberately NO PKG_CONFIG_SYSROOT_DIR. Our .pc files bake the real
# final prefix path, and the sysroot prefix breaks `pkg-config --variable=...`
# consumers (e.g. libxcb's build needs xcb-proto's xcbincludedir verbatim;
# with a sysroot set it comes out doubled and the build fails).
export PKG_CONFIG_ALLOW_SYSTEM_CFLAGS=1

# Host-side xfce4-dev-tools (xdt-gen-visibility, xdt-csource): libxfce4util's
# meson requires xdt-gen-visibility unconditionally at setup time. These are
# arch-independent scripts; build_xfce_libs.sh installs them into
# $CROSSDIR/host-tools if absent.
if [ -d "$CROSSDIR/host-tools/bin" ]; then
  export PATH="$CROSSDIR/host-tools/bin:$PATH"
fi

# GCC 15 turns a pile of legacy-C warnings into hard errors; the xorg scripts
# already carry this suppression set (see build_pcmanfm.sh).
BASE_CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-return-mismatch -fcommon"
# Host gdbus-codegen (glib 2.88) emits g_variant_builder_init_static(),
# introduced in glib 2.84 — newer than the target glib (2.80). The _static
# variant is a drop-in performance alias of g_variant_builder_init().
BASE_CFLAGS="$BASE_CFLAGS -Dg_variant_builder_init_static=g_variant_builder_init"
export CFLAGS="${CFLAGS:-$BASE_CFLAGS}"
export CXXFLAGS="${CXXFLAGS:-$BASE_CFLAGS}"
# Autotools packages search their dependencies with the compiler, not
# pkg-config (e.g. libpng configure probing zlibVersion in -lz).
# -rpath-link: this cross ld does NOT consult -rpath when resolving the
# transitive DT_NEEDED of shared libraries given by path (glib's own
# executables failed to find libpcre2/libffi without it) — only -rpath-link.
export CPPFLAGS="-I$PREFIX/include"
export LDFLAGS="-L$PREFIX/lib -Wl,-rpath-link,$PREFIX/lib"

# Meson cross file (shared-library flavour; the dbus script has its own static
# one). Regenerated on every run so it always matches $PREFIX.
CROSS_FILE="$CROSSDIR/xfce-riscv64.ini"
cat > "$CROSS_FILE" <<EOF
[binaries]
c = '${HOST}-gcc'
cpp = '${HOST}-g++'
ar = '${HOST}-ar'
strip = '${HOST}-strip'
pkgconfig = '${CROSSDIR}/pkg-config-cross'
pkg-config = '${CROSSDIR}/pkg-config-cross'

[host_machine]
system = 'linux'
cpu_family = 'riscv64'
cpu = 'riscv64'
endian = 'little'

[properties]
pkg_config_libdir = '${PREFIX}/lib/pkgconfig:${PREFIX}/share/pkgconfig'

[built-in options]
c_args = ['-I${PREFIX}/include']
c_link_args = ['-L${PREFIX}/lib', '-Wl,-rpath-link,${PREFIX}/lib']
cpp_args = ['-I${PREFIX}/include']
cpp_link_args = ['-L${PREFIX}/lib', '-Wl,-rpath-link,${PREFIX}/lib']
EOF

# fetch <tarball-name> <url> [fallback-url...]
# Keeps a persistent copy in $MIRROR (target/ is volatile).
fetch() {
  local tar="$1"; shift
  if [ -f "$MIRROR/$tar" ]; then
    cp -f "$MIRROR/$tar" "$SRC/$tar"
    return 0
  fi
  local url
  for url in "$@"; do
    echo "  downloading $tar from $url" >&2
    if curl -fsSL --retry 6 --retry-all-errors --retry-delay 2 -o "$SRC/$tar" "$url"; then
      cp -f "$SRC/$tar" "$MIRROR/$tar"
      return 0
    fi
    rm -f "$SRC/$tar"
  done
  echo "FATAL: could not download $tar" >&2
  return 1
}

# srcdir <tarball-name> <url>... -> extracts into $SRC and prints the dir name
srcdir() {
  local tar="$1"; shift
  local dir="${tar%.tar.*}"
  if [ ! -d "$SRC/$dir" ]; then
    fetch "$tar" "$@"
    echo "  extracting $tar" >&2
    tar -C "$SRC" -xf "$SRC/$tar"
  fi
  echo "$dir"
}

# mopt <meson_options.txt> <option> <value> — emit -D<option>=<value> only if
# the option exists in this version (keeps one script working across upstream
# option renames).
mopt() {
  if grep -q "'$2'" "$1" 2>/dev/null; then printf " -D%s=%s" "$2" "$3"; fi
}

# copt <flag> — emit a configure flag only if this configure supports it.
# Matches the option key regardless of polarity (configure --help usually
# lists only one of --enable-X / --disable-X). Must be called with
# ./configure present in cwd.
copt() {
  local key="${1#--}"
  key="${key#disable-}"; key="${key#enable-}"
  key="${key#without-}";  key="${key#with-}"
  if ./configure --help 2>/dev/null | grep -q -- "$key"; then printf " %s" "$1"; fi
}

# done/need markers for idempotent re-runs
mark_done() { touch "$LOGS/.done-$1"; }
is_done()   { [ -f "$LOGS/.done-$1" ]; }

run_pkg() { # run_pkg <name> <build-function>
  local name="$1"; shift
  if is_done "$name"; then
    echo "=== $name: already built (skip) ==="
    return 0
  fi
  echo "=== $name: building (log: $LOGS/$name.log) ==="
  if "$@" > "$LOGS/$name.log" 2>&1; then
    mark_done "$name"
    echo "=== $name: OK ==="
  else
    echo "=== $name: FAILED — see $LOGS/$name.log ===" >&2
    tail -25 "$LOGS/$name.log" >&2
    exit 1
  fi
}

MESON_SETUP() { # MESON_SETUP <dir> <extra args...>
  local dir="$1"; shift
  rm -rf "$dir/build-riscv"
  # shellcheck disable=SC2046
  meson setup "$dir/build-riscv" "$dir" --cross-file "$CROSS_FILE" \
    --prefix="$PREFIX" --libdir=lib --buildtype=release "$@"
}

NINJA_INSTALL() {
  ninja -C "$1" -j"$JOBS" && ninja -C "$1" install
}
