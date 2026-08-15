#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-compile the GTK2 pcmanfm file manager (+ its libfm core) for riscv64.
#
# pcmanfm 1.3.2 is the last GTK2 release of the LXDE file manager. Unlike
# lxpanel/matchbox-panel it is monolithic (no .so plugin architecture), so it
# links as one static GTK2 binary the same way gtk-hello does. Its only hard
# dependencies are glib/gio-unix/gtk+-2.0/pango/cairo, all already in the cross
# tree. Optional deps (menu-cache, gvfs, libexif, dbus-glib, vala, gtk-doc,
# intltool) are disabled or stubbed.
#
# The git tags ship no pre-generated configure, and the host has neither
# gtk-doc nor intltool installed. autoreconf would refuse to run (it auto-runs
# intltoolize/gtkdocize when it sees IT_PROG_INTLTOOL/GTK_DOC_CHECK), and
# aclocal would fail to resolve those macros. So we provide:
#   * no-op `intltoolize` + `gtkdocize` executables on PATH, and
#   * no-op `intltool.m4` + `gtk-doc.m4` stubs on ACLOCAL_PATH (works for both
#     packages regardless of AC_CONFIG_MACRO_DIR), and
#   * a no-op po/Makefile.in.in (same trick as build_matchbox_panel.sh).
set -uo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
JOBS="$(nproc)"

STUB="$ROOT/target/riscv-cross/autotools-stubs"
mkdir -p "$STUB/bin" "$STUB/aclocal"

# No-op intltoolize/gtkdocize so autoreconf does not abort.
for t in intltoolize gtkdocize; do
    cat > "$STUB/bin/$t" <<'EOF'
#!/bin/sh
exit 0
EOF
    chmod +x "$STUB/bin/$t"
done

# glib-genmarshal / glib-mkenums are host-runnable Python scripts installed
# into the cross prefix's usr/bin (which otherwise holds riscv64 target tools).
# libfm's src/Makefile.am invokes `glib-genmarshal` by bare name, so expose the
# host scripts on PATH via $STUB/bin rather than adding the whole cross bin/.
for t in glib-genmarshal glib-mkenums; do
    if [ -f "$PREFIX/bin/$t" ]; then
        ln -sf "$PREFIX/bin/$t" "$STUB/bin/$t"
    fi
done

# gtk-doc m4 (host lacks gtk-doc): GTK_DOC_CHECK -> no-op.
cat > "$STUB/aclocal/gtk-doc.m4" <<'EOF'
AC_DEFUN([GTK_DOC_CHECK], [
  AC_ARG_ENABLE([gtk-doc], [AS_HELP_STRING([--enable-gtk-doc],[use gtk-doc])],
                [enable_gtk_doc="$enableval"], [enable_gtk_doc=no])
  AC_SUBST([GTKDOC_CHECK], [:])
  AC_SUBST([GTKDOC_MKPDF], [:])
  AC_SUBST([GTKDOC_REBASE], [:])
  AM_CONDITIONAL([ENABLE_GTK_DOC], [test x"$enable_gtk_doc" = xyes])
])
EOF

# intltool m4 (host lacks intltool): IT_PROG_INTLTOOL -> no-op that still
# defines the *RULE substitution variables the .am files reference. The recipes
# copy the .in template to the target (intltool-merge would translate the _()
# strings; without translations a straight copy is equivalent for our build).
cat > "$STUB/aclocal/intltool.m4" <<'EOF'
AC_DEFUN([IT_PROG_INTLTOOL], [
  INTLTOOL_DESKTOP_RULE='%.desktop:   %.desktop.in   ; cp $< [$]@'
  INTLTOOL_DIRECTORY_RULE='%.directory: %.directory.in ; cp $< [$]@'
  INTLTOOL_KEY_RULE='%.key: %.key.in ; cp $< [$]@'
  INTLTOOL_XML_RULE='%.xml:   %.xml.in   ; cp $< [$]@'
  AC_SUBST([INTLTOOL_DESKTOP_RULE])
  AC_SUBST([INTLTOOL_DIRECTORY_RULE])
  AC_SUBST([INTLTOOL_KEY_RULE])
  AC_SUBST([INTLTOOL_XML_RULE])
  AC_SUBST([INTLTOOL_MERGE], [true])
  AC_SUBST([INTLTOOL_EXTRACT], [true])
  AC_SUBST([INTLTOOL_UPDATE], [true])
  AC_SUBST([INTLTOOL_PERL], [true])
])
EOF

export PATH="$STUB/bin:$PATH"
export CC="$HOST-gcc"
export PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"
export ACLOCAL_PATH="$STUB/aclocal:$PREFIX/share/aclocal:/usr/share/aclocal"
# GCC 15 turns several legacy-C warnings into errors.
export CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-return-mismatch -fcommon"
export LDFLAGS="-L$PREFIX/lib"

write_stubs() {
    local dir="$1"
    mkdir -p "$dir/po"
    # gtk-doc.make is included unconditionally by libfm's docs Makefile.am
    # (docs/reference/libfm/Makefile.am:131) and menu-cache's docs Makefile.am.
    # gtkdocize would normally copy it; since gtk-doc is stubbed out, provide a
    # fragment that only defines EXTRA_DIST (the one var those Makefile.ams
    # `+=` after the include without first `=`-defining it). Defining anything
    # else here (e.g. DISTCLEANFILES) trips automake's -Werror "multiply
    # defined" warning. The doc targets are guarded by ENABLE_GTK_DOC=no.
    cat > "$dir/gtk-doc.make" <<'EOF'
EXTRA_DIST =
EOF
    # gettext po template (glib-gettext.m4's AM_GLIB_GNU_GETTEXT needs it).
    cat > "$dir/po/Makefile.in.in" <<'EOF'
PACKAGE = @PACKAGE@
VERSION = @VERSION@
prefix = @prefix@
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
}

build_one() {
    local name="$1"
    local dir="$2"
    local extra_args="$3"
    local force="${4:-0}"

    write_stubs "$dir"
    cd "$dir"

    if [ ! -f configure ]; then
        echo "== $name: autoreconf =="
        autoreconf -fi --warnings=none 2>&1 | tail -8
    fi

    if [ "$force" = "1" ] || [ ! -f Makefile ]; then
        [ "$force" = "1" ] && { make distclean >/dev/null 2>&1 || true; }
        echo "== $name: configure =="
        ./configure --host="$HOST" --prefix="$PREFIX" \
            --disable-shared --enable-static \
            --disable-nls --with-gtk=2.0 \
            $extra_args 2>&1 | tail -30
    fi

    echo "== $name: make =="
    make -j"$JOBS" 2>&1 | tail -30
    make install 2>&1 | tail -5
}

mkdir -p "$SRC"

for r in menu-cache:1.1.1 libfm:1.3.2 pcmanfm:1.3.2; do
    name="${r%%:*}"
    ver="${r##*:}"
    if [ ! -d "$SRC/$name-$ver" ]; then
        echo "== cloning $name $ver =="
        git clone --depth 1 --branch "$ver" "https://github.com/lxde/$name.git" "$SRC/$name-$ver" 2>&1 | tail -2
    fi
done

# libfm's src/modules builds gio runtime plugins (vfs-menu.la, gtk-fileprop-*.la,
# gtk-menu-*.la) with libtool `-module -shared`; these cannot be built in a
# static build (same plugin model that blocks matchbox-panel). Drop the `modules`
# and `tests` subdirs — pcmanfm only needs the monolithic libfm + libfm-gtk.
# Idempotent: `git clean` does not revert tracked-file edits.
sed -i '/^\tmodules \\$/d; /^\ttests \\$/d' "$SRC/libfm-1.3.2/src/Makefile.am"

# libfm.pc.in omits its hard menu-cache dependency, so a static pcmanfm link
# drops -lmenu-cache and fails with undefined menu_cache_* symbols. Append it.
sed -i 's/^Requires: .*/& libmenu-cache/' "$SRC/libfm-1.3.2/libfm.pc.in"

# Dependency chain (each package's configure.ac hard-requires the previous):
#   libfm-extra -> menu-cache -> libfm (full) -> pcmanfm
#
# 1. libfm-extra only (--with-extra-only skips the menu-cache PKG_CHECK).
build_one "libfm-extra" "$SRC/libfm-1.3.2" \
    "--with-extra-only --disable-demo --disable-old-actions --disable-exif --disable-udisks"
# 2. menu-cache (needs libfm-extra; no intltool/gettext, only optional gtk-doc).
build_one "menu-cache" "$SRC/menu-cache-1.1.1" "--disable-gtk-doc"
# 3. libfm full (now menu-cache is installed) — force a reconfigure.
build_one "libfm" "$SRC/libfm-1.3.2" \
    "--disable-demo --disable-old-actions --disable-exif --disable-udisks" 1
# 4. pcmanfm (needs libfm + libfm-gtk).
build_one "pcmanfm" "$SRC/pcmanfm-1.3.2" ""

echo "== result =="
for b in menu-cache libfm pcmanfm; do
    if [ -f "$PREFIX/bin/$b" ]; then
        "$HOST-strip" "$PREFIX/bin/$b" 2>/dev/null
        echo "OK   $PREFIX/bin/$b ($(stat -c%s "$PREFIX/bin/$b") bytes stripped)"
    else
        echo "MISS $PREFIX/bin/$b"
    fi
done
