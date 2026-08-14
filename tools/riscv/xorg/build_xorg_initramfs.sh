#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build an initramfs that launches Xorg (fbdev + evdev) on the Asterinas RISC-V
# framebuffer chain. Assumes the xorg-server and driver dependency chain has
# been cross-compiled into target/riscv-cross/usr (see build_wayland_deps.sh
# pattern; the Xorg chain is built manually via the xorg source trees).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CROSS_USR="${REPO_ROOT}/target/riscv-cross/usr"
SYSROOT_LIB="/usr/riscv64-linux-gnu/lib"
OUTPUT="${1:-${REPO_ROOT}/target/qemu-uboot/initramfs-xorg.cpio.gz}"
ROOTFS="${REPO_ROOT}/target/xorg-rootfs"

CC="riscv64-linux-gnu-gcc"

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/lib" "${ROOTFS}/usr/lib" "${ROOTFS}/usr/bin" \
    "${ROOTFS}/usr/lib/xorg/modules/drivers" \
    "${ROOTFS}/usr/lib/xorg/modules/input" "${ROOTFS}/etc" \
    "${ROOTFS}/usr/share/X11/xkb" "${ROOTFS}/etc/fonts" "${ROOTFS}/usr/share/fonts" \
    "${ROOTFS}/bin" "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

# /init launcher (static).
"${CC}" -O2 -static -no-pie -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

# Dynamic linker + glibc runtime.
cp "${SYSROOT_LIB}/ld-linux-riscv64-lp64d.so.1" "${ROOTFS}/lib/"
for lib in libc.so.6 libm.so.6 libdl.so.2 librt.so.1 libpthread.so.0 libgcc_s.so.1; do
    cp "${SYSROOT_LIB}/${lib}" "${ROOTFS}/lib/"
done

# BusyBox (static riscv64) as /bin/sh. Xorg compiles keymaps via popen(), which
# execs "/bin/sh -c <xkbcomp ...>"; without a shell that exec fails and Xorg
# aborts with "Failed to activate virtual core keyboard". Built from
# busybox-1.36.1 (allnoconfig + ash + static) in target/riscv-cross/src.
BUSYBOX_SRC="${CROSS_USR}/bin/busybox"
if [ -f "${BUSYBOX_SRC}" ]; then
    cp "${BUSYBOX_SRC}" "${ROOTFS}/bin/busybox"
    ln -sf busybox "${ROOTFS}/bin/sh"
else
    echo "WARNING: ${BUSYBOX_SRC} not found; Xorg keyboard init will fail" >&2
fi

# libxcvt (Xorg's only non-glibc dynamic dependency).
cp "${CROSS_USR}/lib/libxcvt.so.0.1.3" "${ROOTFS}/usr/lib/libxcvt.so.0.1.3"
ln -sf libxcvt.so.0.1.3 "${ROOTFS}/usr/lib/libxcvt.so.0"

# Xorg binary and all its loadable modules (fbdevhw, shadow, drivers, input).
cp "${CROSS_USR}/bin/Xorg" "${ROOTFS}/usr/bin/Xorg"
cp "${CROSS_USR}/lib/xorg/modules/"*.so "${ROOTFS}/usr/lib/xorg/modules/"
cp "${CROSS_USR}/lib/xorg/modules/drivers/fbdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/drivers/"
cp "${CROSS_USR}/lib/xorg/modules/input/evdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/input/"

# Desktop session clients (statically-linked riscv64 X11 apps).
# NOTE: pcmanfm (build_pcmanfm.sh) and xterm cross-compile, but their binaries
# push the initramfs past the kernel's ~20 MB early-memory limit (the kernel
# stalls at "Spawn the first kernel thread"), so they are intentionally not
# bundled in the default session (see GTK-M2-report.md). xwm/xclient are the
# superseded pre-matchbox demos and are no longer spawned by init.c.
for cli in gtk-hello matchbox-window-manager xpanel; do
    if [ -f "${CROSS_USR}/bin/${cli}" ]; then
        cp "${CROSS_USR}/bin/${cli}" "${ROOTFS}/usr/bin/${cli}"
    else
        echo "WARNING: ${CROSS_USR}/bin/${cli} not found; skipping" >&2
    fi
done

# xkbcomp (statically-linked riscv64). Xorg compiles keymaps by shelling out to
# xkbcomp at its configure-time XKB_BIN_DIRECTORY, which is the host cross
# prefix. We place the binary at that exact path inside the guest rootfs so the
# server can find it. Built from xkbcomp-1.4.7 against the cross tree's
# libxkbfile/libX11 (see target/riscv-cross/src/xkbcomp-1.4.7).
XKBCOMP_SRC="${CROSS_USR}/bin/xkbcomp"
XKBCOMP_BAKED="/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr/bin"
if [ -f "${XKBCOMP_SRC}" ]; then
    mkdir -p "${ROOTFS}${XKBCOMP_BAKED}"
    cp "${XKBCOMP_SRC}" "${ROOTFS}${XKBCOMP_BAKED}/xkbcomp"
else
    echo "WARNING: ${XKBCOMP_SRC} not found; Xorg keyboard init will fail" >&2
fi

# Config.
cp "${SRC_DIR}/xorg.conf" "${ROOTFS}/etc/xorg.conf"

# xkeyboard-config data (XKB rules/symbols/keycodes/types/compat/geometry).
# These are architecture-independent text files, so we copy them from the host
# install. Xorg's evdev driver needs them to compile a keymap; without them the
# server aborts with "Failed to activate virtual core keyboard".
HOST_XKB="/usr/share/X11/xkb"
if [ -d "${HOST_XKB}" ]; then
    cp -rL "${HOST_XKB}/." "${ROOTFS}/usr/share/X11/xkb/"
else
    echo "WARNING: ${HOST_XKB} not found; Xorg keyboard init will fail" >&2
fi

# Fonts + fontconfig config for GTK2/pango text rendering.
# AdwaitaSans-Regular.ttf is a small, readable sans font; fonts.conf maps the
# generic sans-serif/serif/monospace families onto it.
HOST_FONT="/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf"
if [ -f "${HOST_FONT}" ]; then
    cp "${HOST_FONT}" "${ROOTFS}/usr/share/fonts/AdwaitaSans-Regular.ttf"
else
    echo "WARNING: ${HOST_FONT} not found; GTK2 text will be empty" >&2
fi
cp "${SRC_DIR}/fonts.conf" "${ROOTFS}/etc/fonts/fonts.conf"

# terminfo database for xterm. The full ncurses DB is ~12 MB of entries we
# never use (xterm, linux, vt100 fallbacks are compiled into libtinfo via
# --with-fallbacks). Shipping only the `x`/ directory keeps TERMINFO lookups
# working for xterm while keeping the initramfs under the kernel's
# early-memory limit (the full DB pushed it over and stalled the boot at
# "Spawn the first kernel thread").
if [ -d "${CROSS_USR}/share/terminfo/x" ]; then
    mkdir -p "${ROOTFS}/usr/share/terminfo/x"
    cp -r "${CROSS_USR}/share/terminfo/x/." "${ROOTFS}/usr/share/terminfo/x/"
fi

# Pack as newc cpio + gzip.
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
