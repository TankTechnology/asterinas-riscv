#!/usr/bin/env bash
# Cross-compile NetSurf 3.9 (GTK2 frontend) for the riscv64 desktop.
#
# NetSurf's GTK frontend is the last piece of the desktop. This script builds
# the NetSurf core libraries (libcss/libdom/libhubbub/libparserutils/...) as
# static riscv64 .a files, then the `nsgtk` GTK2 frontend binary, installing
# everything into target/riscv-cross/usr alongside the existing GTK2 stack.
#
# Unlike the autotools GTK stack, NetSurf uses its own GNU-make buildsystem
# (bundled in target/riscv-cross/src/netsurf-all-3.9). Two different cross
# conventions apply:
#   * libraries:  HOST=<target ABI>  (e.g. riscv64-linux-gnu), NSSHARED=<buildsystem>
#   * frontend:   HOST is `uname -s` (build platform), cross is via CC/PKG_CONFIG
#
# Network (libcurl/openssl) is DISABLED: the milestone renders a local HTML
# file via file://, so no HTTP/HTTPS fetch is needed. SVG (libsvgtiny) and
# RISC OS sprites (librosprite) are enabled; WEBP/RSVG/JS/GRESOURCE are off.
set -uo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
BUNDLE="$SRC/netsurf-all-3.9"
HOST=riscv64-linux-gnu
BUILD=x86_64-linux-gnu
NSSHARED="$BUNDLE/buildsystem"
JOBS="$(nproc)"
LOG="$ROOT/target/riscv-cross/src/netsurf-build.log"

# Cross toolchain + pkg-config. The pkg-config-static wrapper adds --static so
# the frontend pulls in Requires.private/Libs.private when linking statically.
# AR/CXX must be set explicitly: the buildsystem's tool-prefix derivation
# (Makefile.tools `toolprefix_`) mangles riscv64-linux-gnu into
# /usr/bin/riscv64/linux/gnu/-ar when it has to guess the binutils.
export CC="$HOST-gcc"
export CXX="$HOST-g++"
export AR="$HOST-ar"
export BUILD_CC="gcc"
export PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"
# NetSurf's library buildsystem uses PKGCONFIG (not PKG_CONFIG).
export PKGCONFIG="$ROOT/target/riscv-cross/pkg-config-static"
# GCC 15 turns legacy-C warnings into errors; -fcommon for the old codebase.
export CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-return-mismatch -fcommon"
export LDFLAGS="-L$PREFIX/lib"

# NetSurf core libraries in dependency order (the gtk target set from the
# bundle's top-level Makefile, minus buildsystem which is used in-tree).
LIBS="libnslog libwapcaplet libparserutils libcss libhubbub libdom libnsbmp libnsgif librosprite libnsutils libutf8proc libnspsl libsvgtiny"

build_lib() {
    local lib="$1"
    echo "==== [$lib] ===="
    make -C "$BUNDLE/$lib" install \
        HOST="$HOST" \
        NSSHARED="$NSSHARED" \
        PREFIX="$PREFIX" \
        COMPONENT_TYPE=lib-static \
        WARNFLAGS="-Wno-error" \
        -j"$JOBS" \
        || { echo "FAILED: $lib"; return 1; }
}

build_libs() {
    for lib in $LIBS; do
        build_lib "$lib" || return 1
    done
}

# NetSurf frontend config: network off, image/format features on where present.
write_frontend_config() {
    cat > "$BUNDLE/netsurf/Makefile.config" <<'EOF'
# Downstream riscv64 static GTK2 build (NOT upstream).
override NETSURF_USE_CURL := NO
override NETSURF_USE_OPENSSL := NO
override NETSURF_USE_RSVG := NO
override NETSURF_USE_NSSVG := YES
override NETSURF_USE_ROSPRITE := YES
override NETSURF_USE_WEBP := NO
override NETSURF_USE_DUKTAPE := NO
override NETSURF_USE_GRESOURCE := NO
override NETSURF_USE_INLINE_PIXBUF := YES
override NETSURF_USE_NSPSL := YES
override NETSURF_USE_NSLOG := YES
override NETSURF_USE_HARU_PDF := NO
override NETSURF_USE_VIDEO := NO
override NETSURF_STRIP_BINARY := YES
EOF
}

build_frontend() {
    write_frontend_config
    echo "==== [netsurf gtk2 frontend] ===="
    # The INLINE_PIXBUF rule (frontends/gtk/Makefile) writes favicon.c etc. into
    # $(OBJROOT) without depending on the $(OBJROOT)/created dir, so under -j it
    # can run before any object file has made the directory. Pre-create it.
    mkdir -p "$BUNDLE/netsurf/build/Linux-gtk" "$BUNDLE/netsurf/build/Linux-gtk/deps"
    # Static GTK + GtkBuilder: the .ui files reference widget types (GtkStatusbar,
    # GtkHPaned, GtkLayout, …) that NetSurf never calls directly, so the static
    # linker drops their objects (e.g. gtkstatusbar.o) and GtkBuilder's lazy type
    # resolution (g_module_symbol("gtk_statusbar_get_type")) fails at runtime with
    # "Invalid object type `GtkStatusbar'". --whole-archive forces every libgtk
    # object in; --export-dynamic exposes the *_get_type symbols to dlsym.
    export LDFLAGS="-L$PREFIX/lib -Wl,--export-dynamic -Wl,--whole-archive -lgtk-x11-2.0 -Wl,--no-whole-archive"
    make -C "$BUNDLE/netsurf" \
        TARGET=gtk \
        CC="$HOST-gcc" \
        CXX="$HOST-g++" \
        AR="$HOST-ar" \
        STRIP="$HOST-strip" \
        PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static" \
        -j"$JOBS" \
        || { echo "FAILED: netsurf frontend"; return 1; }
    # Install binary + GTK resources into the cross prefix.
    make -C "$BUNDLE/netsurf" \
        TARGET=gtk \
        CC="$HOST-gcc" \
        PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static" \
        PREFIX="$PREFIX" \
        install \
        || { echo "FAILED: netsurf install"; return 1; }
    # The upstream install-gtk target omits the `accelerators` resource from
    # GTK_RESOURCES_LIST. It is only needed as a *file* when the gresource is
    # disabled (NETSURF_USE_GRESOURCE := NO) — otherwise it lives inside the
    # compiled gresource. With it missing, nsgtk_init_resources() fails with
    # "Unable to find resource accelerators", nsgtk exits status 1, and the
    # systemd Restart=always loop keeps respawning it before it ever renders a
    # page. Install it explicitly so the file-based resource path is complete.
    install -m 0644 "$BUNDLE/netsurf/frontends/gtk/res/accelerators" \
        "$PREFIX/share/netsurf/accelerators"
}

verify() {
    echo "==== verify ===="
    rc=0
    for lib in $LIBS; do
        # library name == component name, but a few differ
        case "$lib" in
            libnslog) pc=libnslog ;;
            libwapcaplet) pc=libwapcaplet ;;
            libparserutils) pc=libparserutils ;;
            libcss) pc=libcss ;;
            libhubbub) pc=libhubbub ;;
            libdom) pc=libdom ;;
            libnsbmp) pc=libnsbmp ;;
            libnsgif) pc=libnsgif ;;
            librosprite) pc=librosprite ;;
            libnsutils) pc=libnsutils ;;
            libutf8proc) pc=libutf8proc ;;
            libnspsl) pc=libnspsl ;;
            libsvgtiny) pc=libsvgtiny ;;
        esac
        if [ -f "$PREFIX/lib/$pc.a" ]; then
            echo "OK   $pc.a"
        else
            echo "MISS $pc.a"
            rc=1
        fi
    done
    if [ -f "$BUNDLE/netsurf/nsgtk" ]; then
        echo "OK   nsgtk ($(stat -c%s "$BUNDLE/netsurf/nsgtk") bytes)"
    else
        echo "MISS nsgtk"
        rc=1
    fi
    exit "$rc"
}

case "${1:-all}" in
    build_libs) build_libs ;;
    build_frontend) build_frontend ;;
    verify) verify ;;
    all) build_libs && build_frontend && verify ;;
    *) echo "usage: $0 {build_libs|build_frontend|verify|all}"; exit 2 ;;
esac
