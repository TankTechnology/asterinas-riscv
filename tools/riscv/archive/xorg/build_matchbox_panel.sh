#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-compile the GTK2 matchbox-panel against the static glib+GTK2 stack.
#
# RESULT OF THIS ATTEMPT (2026-08-14): the panel *core* binary builds and
# links statically (matchbox-panel/matchbox-panel, ~80M -> ~14M stripped), but
# matchbox-panel is NOT runnable in this environment, for two architectural
# reasons that this script documents:
#
#   1. The applets are libtool *modules* (`applet_LTLIBRARIES = libclock.la`
#      ... `-avoid-version -module`), loaded at runtime via g_module_open().
#      With `--disable-shared` they are emitted as static `.a` archives, not
#      `.so` plugins, so they cannot be dlopen'ed.
#
#   2. matchbox-panel/mb-panel.c:287 bails out immediately unless
#      `g_module_supported()` returns true, and that returns FALSE for a
#      fully-static glibc binary (no dlopen). So the static panel core exits
#      with "gmodule support not found" before ever reaching gtk_main().
#
# A real matchbox-panel (or lxpanel, which has the same GTK2 plugin model)
# would require rebuilding the entire glib/GTK2 stack as *shared* libraries,
# plus it would sit on the same glib stack that already crashed openbox 3.6.1
# at runtime. We therefore stay on the pure-X11 path (matchbox-window-manager
# + a hand-written X11 panel, see xpanel.c).
#
# This script is kept so the recipe (fetch -> autoreconf -> po stub ->
# configure -> build core) is reproducible if we later revisit a shared-GTK
# build. It does NOT `set -e` on the final `make`, because the applet
# test-linkage step is expected to fail under --disable-shared.
set -uo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
JOBS="$(nproc)"
REPO=https://git.yoctoproject.org/matchbox-panel-2
TAG=gtk2        # the GTK2 (v2.0) line; tag 2.11 is the GTK3 rewrite
DIR="$SRC/matchbox-panel-gtk2"

export CC="$HOST-gcc"
export PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"
export GLIB_MKENUMS="$PREFIX/bin/glib-mkenums"
export GLIB_GENMARSHAL="$PREFIX/bin/glib-genmarshal"
export ACLOCAL_PATH="$PREFIX/share/aclocal:/usr/share/aclocal"
# GCC 15 turns several legacy-C warnings into errors; GTK2-era code trips them.
export CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-return-mismatch -fcommon"
export LDFLAGS="-L$PREFIX/lib"

mkdir -p "$SRC"

if [ ! -d "$DIR" ]; then
    echo "== cloning $REPO ($TAG) =="
    git clone --depth 1 --branch "$TAG" "$REPO" "$DIR"
fi
cd "$DIR"

# The git tag has no pre-generated configure; autoreconf builds it.
if [ ! -f configure ]; then
    echo "== autoreconf =="
    autoreconf -fi
fi

# glib-gettextize can't run (host glib2 was installed without its gettext
# templates and the cross glib was built -Dnls=disabled). Provide a no-op
# po/Makefile.in.in so config.status can process po/Makefile.in.
if [ ! -f po/Makefile.in.in ]; then
    echo "== writing po stub =="
    cat > po/Makefile.in.in <<'EOF'
PACKAGE = @PACKAGE@
VERSION = @VERSION@
all:
install:
install-data:
install-exec:
install-strip:
uninstall:
check:
installcheck:
clean:
mostlyclean:
distclean:
maintainer-clean:
	rm -f Makefile
dist:
distdir:
TAGS:
EOF
fi

if [ ! -f Makefile ]; then
    echo "== configure =="
    ./configure --host="$HOST" --prefix="$PREFIX" \
        --disable-shared --enable-static \
        --disable-startup-notification --disable-dbus --with-battery=none
fi

echo "== build panel core only (proves the GTK2 stack links) =="
make -C matchbox-panel -j"$JOBS" matchbox-panel

if [ -f matchbox-panel/matchbox-panel ]; then
    "$HOST-strip" matchbox-panel/matchbox-panel
    echo "OK   matchbox-panel/matchbox-panel ($(stat -c%s matchbox-panel/matchbox-panel) bytes stripped)"
else
    echo "MISS matchbox-panel/matchbox-panel"
fi

cat <<'EOF'
NOTE: the full `make` (with applets) is expected to fail at applets/*/test-linkage
under --disable-shared, and even a successful build cannot run because
matchbox-panel requires g_module_supported() (dlopen), which static binaries
lack. See the header comment.
EOF
