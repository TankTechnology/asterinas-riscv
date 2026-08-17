#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# SYSTEMD-DESKTOP-M1: assemble an initramfs that boots systemd (v257.5, riscv64
# glibc) as PID 1 on Asterinas RISC-V and starts a graphical desktop session
# (Xorg + matchbox-window-manager + xpanel + pcmanfm + xterm) via systemd units
# under graphical.target.
#
# This is a direct extension of the sibling tree's proven SYSTEMD-BOOT rootfs
# (see /home/arch-anjie/Program/asterinas-riscv-nixos/tools/riscv/systemd/
# build_systemd_boot.sh) with the desktop payload layered on top. The desktop
# binaries + data were cross-compiled / assembled into target/riscv-cross/usr by
# the xorg tooling (see tools/riscv/xorg/build_xorg_initramfs.sh and the GTK-M*
# reports).
#
# The output is a raw newc cpio (no gzip). The kernel's zune-inflate decoder
# hangs non-deterministically on >16 MB gzip inputs, and this rootfs is far
# larger than that (systemd is ~45 MB unstripped before we strip it), so raw
# cpio is the only reliable packing format.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/systemd-desktop"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${REPO_ROOT}/target/qemu-uboot/systemd-desktop-initramfs.cpio"

NO_PACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-pack) NO_PACK=1 ;;
        *) OUTPUT="$1" ;;
    esac
    shift
done

# systemd was cross-compiled into this tree (878/878 targets, see
# tools/riscv/systemd/SYSTEMD-M2-report.md). The desktop binaries and their
# runtime data live in the same cross prefix.
SD_BUILD="${REPO_ROOT}/target/riscv-cross/src/systemd-257.5/build-riscv"
CROSS_USR="${REPO_ROOT}/target/riscv-cross/usr"
GLIBC_LIB="${REPO_ROOT}/target/xorg-rootfs/lib"   # proven glibc 2.41 runtime
SYSROOT_LIB="/usr/riscv64-linux-gnu/lib"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
STRIP="${RISC_V_STRIP:-riscv64-linux-gnu-strip}"
BUSYBOX="${CROSS_USR}/bin/busybox"

[[ -d "${SD_BUILD}" ]] || { echo "missing systemd build: ${SD_BUILD}" >&2; exit 2; }
[[ -d "${GLIBC_LIB}" ]] || { echo "missing glibc runtime: ${GLIBC_LIB}" >&2; exit 2; }

echo "=== assembling systemd + desktop rootfs ==="
rm -rf "${ROOTFS}"
mkdir -p \
    "${ROOTFS}/lib" \
    "${ROOTFS}/usr/lib/systemd" \
    "${ROOTFS}/usr/lib/xorg/modules/drivers" \
    "${ROOTFS}/usr/lib/xorg/modules/input" \
    "${ROOTFS}/usr/bin" \
    "${ROOTFS}/bin" \
    "${ROOTFS}/etc/systemd/system" \
    "${ROOTFS}/etc/fonts" \
    "${ROOTFS}/usr/share/fonts" \
    "${ROOTFS}/usr/share/X11/xkb" \
    "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp" \
    "${ROOTFS}/run" "${ROOTFS}/var/log" "${ROOTFS}/var/tmp" \
    "${ROOTFS}/root" "${ROOTFS}/home" "${ROOTFS}/mnt" "${ROOTFS}/srv" \
    "${ROOTFS}/sys/fs/cgroup"

# 1. Static /init launcher (becomes PID 1, exec()s systemd).
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

# 2. glibc dynamic runtime (the exact closure the xorg-rootfs image proved
#    works with this kernel's ELF loader: glibc 2.41, riscv64-linux-gnu 15.1).
for lib in ld-linux-riscv64-lp64d.so.1 libc.so.6 libm.so.6 \
           libdl.so.2 librt.so.1 libpthread.so.0 libgcc_s.so.1; do
    cp "${GLIBC_LIB}/${lib}" "${ROOTFS}/lib/"
done

# 3. systemd pid1 + every helper binary it was cross-built with (69 ELF
#    executables). The binary paths are baked into config.h as the *host*
#    prefix (this build was never `meson install`ed), so we bridge that with a
#    symlink so every baked path resolves to the guest's canonical /usr (see
#    the sibling SYSTEMD-BOOT report). We strip the copies at assembly time:
#    the build tree is unstripped (debug_info) and this rootfs also carries the
#    whole desktop, so stripping keeps it under the initrd load ceiling.
mkdir -p "${ROOTFS}/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross"
ln -sfn /usr "${ROOTFS}/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr"

for f in "${SD_BUILD}"/*; do
    [ -f "$f" ] || continue
    [ -x "$f" ] || continue
    file "$f" | grep -q ELF || continue
    b="$(basename "$f")"
    cp "$f" "${ROOTFS}/usr/lib/systemd/$b"
    "${STRIP}" --strip-unneeded "${ROOTFS}/usr/lib/systemd/$b" 2>/dev/null || true
    ln -sf "../lib/systemd/$b" "${ROOTFS}/usr/bin/$b"
done

# 4. systemd's internal shared libraries (rpath $ORIGIN placeholder that is
#    never rewritten, so they go in /lib, the loader's default search path).
cp "${SD_BUILD}/src/core/libsystemd-core-257.so"   "${ROOTFS}/lib/"
cp "${SD_BUILD}/src/shared/libsystemd-shared-257.so" "${ROOTFS}/lib/"
"${STRIP}" --strip-unneeded "${ROOTFS}/lib/libsystemd-core-257.so" 2>/dev/null || true
"${STRIP}" --strip-unneeded "${ROOTFS}/lib/libsystemd-shared-257.so" 2>/dev/null || true

# 5. busybox helper (static) as /bin/sh + a handful of applet symlinks so the
#    emergency shell, Xorg's keymap popen("/bin/sh -c xkbcomp …"), and any
#    ExecStart=-/bin/sh unit can actually run commands.
if [[ -f "${BUSYBOX}" ]]; then
    cp "${BUSYBOX}" "${ROOTFS}/bin/busybox"
    ln -sf busybox "${ROOTFS}/bin/sh"
    for applet in ls cat echo mount umount mkdir rm ln mknod ps mountpoint \
                  head tail grep find test true false sleep kill sync df free \
                  stty; do
        ln -sf busybox "${ROOTFS}/bin/${applet}"
    done
else
    echo "WARNING: ${BUSYBOX} not found; no helper shell" >&2
fi

# 6. Identity + release files.
cat > "${ROOTFS}/etc/os-release" <<'EOF'
NAME="Asterinas"
ID=asterinas
PRETTY_NAME="Asterinas RISC-V (systemd desktop)"
ANSI_COLOR="0;32"
HOME_URL="https://github.com/asterinas/asterinas"
EOF
printf 'a1b2c3d4e5f60718293a4b5c6d7e8f90\n' > "${ROOTFS}/etc/machine-id"
printf 'asterinas-riscv\n' > "${ROOTFS}/etc/hostname"
cat > "${ROOTFS}/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
nobody:x:65534:65534:nobody:/:/sbin/nologin
EOF
cat > "${ROOTFS}/etc/group" <<'EOF'
root:x:0:
nobody:x:65534:
tty:x:5:
EOF
cat > "${ROOTFS}/etc/hosts" <<'EOF'
127.0.0.1 localhost localhost.localdomain
::1       localhost localhost.localdomain
EOF

# 7. Unit set: default.target -> graphical.target -> multi-user.target ->
#    basic.target, plus the desktop services. All unit files are versioned in
#    tools/riscv/systemd/units/.
cp "${SRC_DIR}"/units/*.target "${SRC_DIR}"/units/*.service \
    "${ROOTFS}/etc/systemd/system/"
ln -sf graphical.target "${ROOTFS}/etc/systemd/system/default.target"

# ---- desktop payload ----------------------------------------------------

# 8. Xorg (dynamic) + its loadable modules (fbdevhw, shadow, drivers, input).
#    Xorg's only non-glibc dynamic dependency is libxcvt.
cp "${CROSS_USR}/bin/Xorg" "${ROOTFS}/usr/bin/Xorg"
cp "${CROSS_USR}/lib/xorg/modules/"*.so "${ROOTFS}/usr/lib/xorg/modules/"
cp "${CROSS_USR}/lib/xorg/modules/drivers/fbdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/drivers/"
cp "${CROSS_USR}/lib/xorg/modules/input/evdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/input/"
cp "${CROSS_USR}/lib/libxcvt.so.0.1.3" "${ROOTFS}/usr/lib/libxcvt.so.0.1.3"
ln -sf libxcvt.so.0.1.3 "${ROOTFS}/usr/lib/libxcvt.so.0"

# 9. Desktop session clients (matchbox-wm/xpanel/pcmanfm/xterm/netsurf-gtk are
#    dynamic or static; all resolve against the glibc runtime in /lib).
for cli in matchbox-window-manager xpanel pcmanfm xterm netsurf-gtk; do
    if [ -f "${CROSS_USR}/bin/${cli}" ]; then
        cp "${CROSS_USR}/bin/${cli}" "${ROOTFS}/usr/bin/${cli}"
    else
        echo "WARNING: ${CROSS_USR}/bin/${cli} not found; skipping" >&2
    fi
done

# 9b. Standalone curl (riscv64, dynamically linked against glibc only — libcurl
#     + libssl/libcrypto are statically linked in). Useful for TLS/certificate
#     behavior tests independent of NetSurf's UI; the curl fetcher in netsurf-gtk
#     is the same libcurl. Compiled with --with-ca-bundle=/etc/ssl/certs/
#     ca-certificates.crt, matching NetSurf's --ca_bundle (BROWSER-M12).
if [ -f "${CROSS_USR}/bin/curl" ]; then
    cp "${CROSS_USR}/bin/curl" "${ROOTFS}/usr/bin/curl"
    "${STRIP}" --strip-unneeded "${ROOTFS}/usr/bin/curl" 2>/dev/null || true
else
    echo "WARNING: ${CROSS_USR}/bin/curl not found; TLS cert tests unavailable" >&2
fi

# 10. xkbcomp (static riscv64). Xorg compiles keymaps by shelling out to xkbcomp
#     at its configure-time XKB_BIN_DIRECTORY (the host cross prefix). The
#     baked-host-path bridge from step 3 maps that to /usr/bin, so we place it
#     there. We ship the xkbcomp-stub (which emits a pre-compiled keymap) as
#     /usr/bin/xkbcomp to avoid the real xkbcomp's shell/pipe overhead.
if [ -f "${SRC_DIR}/xkbcomp-stub" ]; then
    cp "${SRC_DIR}/xkbcomp-stub" "${ROOTFS}/usr/bin/xkbcomp"
elif [ -f "${CROSS_USR}/bin/xkbcomp" ]; then
    cp "${CROSS_USR}/bin/xkbcomp" "${ROOTFS}/usr/bin/xkbcomp"
else
    echo "WARNING: xkbcomp/xkbcomp-stub not found; Xorg keyboard init will fail" >&2
fi

# 10b. Pre-compiled keymap for Xorg's XkbCompiledKeymap option. This lets Xorg
#      load the keymap directly from /etc/xkb/default.xkm without shelling out
#      to xkbcomp at all, avoiding the busybox sh fork overhead.
if [ -f "${SRC_DIR}/default.xkm" ]; then
    mkdir -p "${ROOTFS}/etc/xkb"
    cp "${SRC_DIR}/default.xkm" "${ROOTFS}/etc/xkb/default.xkm"
else
    echo "WARNING: default.xkm not found; Xorg will fall back to xkbcomp" >&2
fi

# 11. xorg.conf (fbdev + evdev) and the XKB rules/symbols/keycodes data.
cp "${SRC_DIR}/../xorg/xorg.conf" "${ROOTFS}/etc/xorg.conf"
HOST_XKB="/usr/share/X11/xkb"
if [ -d "${HOST_XKB}" ]; then
    cp -rL "${HOST_XKB}/." "${ROOTFS}/usr/share/X11/xkb/"
else
    echo "WARNING: ${HOST_XKB} not found; Xorg keyboard init will fail" >&2
fi

# 12. Fonts + fontconfig config for GTK2/pango text rendering.
#     Bundle the Liberation families (Sans/Serif/Mono, each Regular/Bold/Italic/
#     BoldItalic) so pango/GTK2 can resolve real serif + monospace faces and
#     bold/italic variants, rather than collapsing every generic family onto a
#     single Adwaita Sans Regular (the M5 state). Liberation is metrically
#     compatible with Arial/Times/Courier. Keep Adwaita Sans as an extra face.
FONT_SRC="/usr/share/fonts/liberation"
FONT_DST="${ROOTFS}/usr/share/fonts/liberation"
if [ -d "${FONT_SRC}" ]; then
    mkdir -p "${FONT_DST}"
    cp "${FONT_SRC}"/LiberationSans-*.ttf   "${FONT_DST}/" 2>/dev/null
    cp "${FONT_SRC}"/LiberationSerif-*.ttf  "${FONT_DST}/" 2>/dev/null
    cp "${FONT_SRC}"/LiberationMono-*.ttf   "${FONT_DST}/" 2>/dev/null
else
    echo "WARNING: ${FONT_SRC} not found; GTK2 text will be empty" >&2
fi
HOST_FONT="/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf"
if [ -f "${HOST_FONT}" ]; then
    cp "${HOST_FONT}" "${ROOTFS}/usr/share/fonts/AdwaitaSans-Regular.ttf"
fi
cp "${SRC_DIR}/../xorg/fonts.conf" "${ROOTFS}/etc/fonts/fonts.conf"

# 13. pcmanfm / libfm runtime data (builder .ui files, file-type icons, desktop
#     entries) — architecture-independent, needed for the file manager to show
#     icons and dialogs.
for d in pcmanfm libfm applications; do
    if [ -d "${CROSS_USR}/share/${d}" ]; then
        mkdir -p "${ROOTFS}/usr/share/${d}"
        cp -rL "${CROSS_USR}/share/${d}/." "${ROOTFS}/usr/share/${d}/"
    fi
done

# 13b. NetSurf GTK resources (default.css/quirks.css/internal.css, icons,
#      throbber, UI strings, translation Messages) + the local HTML test page.
#      The nsgtk binary's baked GTK_RESPATH is the *host* cross prefix, which the
#      baked-host-path symlink bridge from step 3 maps to /usr/share/netsurf; the
#      NETSURFRES env var in netsurf.service points at the same place.
if [ -d "${CROSS_USR}/share/netsurf" ]; then
    mkdir -p "${ROOTFS}/usr/share/netsurf"
    cp -rL "${CROSS_USR}/share/netsurf/." "${ROOTFS}/usr/share/netsurf/"
fi
cp "${SRC_DIR}/../xorg/netsurf-test.html" "${ROOTFS}/usr/share/netsurf/netsurf-test.html"

# 13c. CA certificate bundle for NetSurf's curl fetcher (TLS verification).
#      The host's ca-certificates store (Mozilla bundle, ~200KB) is
#      architecture-independent; NetSurf is pointed at it via --ca_bundle in
#      netsurf.service, and libcurl's compiled-in default
#      (/etc/ssl/certs/ca-certificates.crt) matches. Actual http(s):// fetch
#      additionally needs a guest network device, which the boot driver does not
#      yet attach (future milestone).
HOST_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
if [ -f "${HOST_CA_BUNDLE}" ]; then
    mkdir -p "${ROOTFS}/etc/ssl/certs"
    cp -L "${HOST_CA_BUNDLE}" "${ROOTFS}/etc/ssl/certs/ca-certificates.crt"
else
    echo "WARNING: ${HOST_CA_BUNDLE} not found; HTTPS cert verification will fail" >&2
fi

# 13d. glibc name-resolution runtime: the resolver (libresolv) + the NSS
#      modules libnss_files (for /etc/hosts) and libnss_dns (for DNS queries),
#      plus nsswitch.conf / resolv.conf. The desktop rootfs only ships the base
#      glibc closure (libc/libm/...); without these, getaddrinfo(3) cannot map a
#      hostname to an address and every networked client (NetSurf's curl
#      fetcher) fails DNS. The nameserver is QEMU slirp's built-in resolver,
#      which the kernel's hardcoded eth0 (10.0.2.15/24, gw 10.0.2.2) reaches
#      through `-netdev user` in the boot driver.
for nss_lib in libnss_files.so.2 libnss_dns.so.2 libresolv.so.2; do
    if [ -f "${SYSROOT_LIB}/${nss_lib}" ]; then
        cp -L "${SYSROOT_LIB}/${nss_lib}" "${ROOTFS}/lib/"
    else
        echo "WARNING: ${SYSROOT_LIB}/${nss_lib} not found; guest DNS will fail" >&2
    fi
done
cat > "${ROOTFS}/etc/nsswitch.conf" <<'EOF'
passwd: files
group:  files
hosts:  files dns
EOF
cat > "${ROOTFS}/etc/resolv.conf" <<'EOF'
nameserver 10.0.2.3
EOF

# 13e. Default-browser experience: a local start/home page, a pre-populated
#      hotlist (bookmarks), and a Choices file pinning the homepage so NetSurf's
#      Home button returns to the bundled start page. These are plain text/HTML,
#      architecture-independent, versioned in tools/riscv/xorg/.
mkdir -p "${ROOTFS}/root/.netsurf"
cp "${SRC_DIR}/../xorg/netsurf-home.html"   "${ROOTFS}/usr/share/netsurf/netsurf-home.html"
cp "${SRC_DIR}/../xorg/netsurf-hotlist.html" "${ROOTFS}/root/.netsurf/Hotlist"
cp "${SRC_DIR}/../xorg/netsurf-choices"      "${ROOTFS}/root/.netsurf/Choices"

# 13f. Local image-render test page + its in-page images (PNG/JPEG/GIF at sizes
#      straddling the 4 KiB speculative-decode threshold). file:// only — no
#      network — so it exercises the in-page <img> decode path deterministically.
if [ -f "${SRC_DIR}/../xorg/netsurf-imagetest.html" ]; then
    cp "${SRC_DIR}/../xorg/netsurf-imagetest.html" "${ROOTFS}/usr/share/netsurf/netsurf-imagetest.html"
    mkdir -p "${ROOTFS}/usr/share/netsurf/imgtest"
    cp "${SRC_DIR}/../xorg/imgtest/"img-* "${ROOTFS}/usr/share/netsurf/imgtest/" 2>/dev/null || true
fi

# 13g. GIF render test: the same GIF as imagetest but placed at the top of the
#      page so it is above the window fold — the M6 imagetest page's GIF row
#      sits below the JPEG row and is clipped off the bottom of the window, which
#      made "GIF absent" unreadable as a decode signal. Deterministic, no network.
if [ -f "${SRC_DIR}/../xorg/netsurf-giftest.html" ]; then
    cp "${SRC_DIR}/../xorg/netsurf-giftest.html" "${ROOTFS}/usr/share/netsurf/netsurf-giftest.html"
fi

# Optional per-site start URL (render-matrix testing): if NETSURF_URL is set in
# the environment, bake it into /etc/netsurf.conf so the unit's EnvironmentFile
# overrides the bundled local home page for network fetch/render tests.
if [ -n "${NETSURF_URL:-}" ]; then
    printf 'NETSURF_URL=%s\n' "${NETSURF_URL}" > "${ROOTFS}/etc/netsurf.conf"
fi

# 14. terminfo database for xterm (the `x`/ directory only; the full ncurses DB
#     is ~12 MB and unnecessary — xterm/linux/vt100 fallbacks are compiled into
#     libtinfo).
if [ -d "${CROSS_USR}/share/terminfo/x" ]; then
    mkdir -p "${ROOTFS}/usr/share/terminfo/x"
    cp -r "${CROSS_USR}/share/terminfo/x/." "${ROOTFS}/usr/share/terminfo/x/"
fi

# 14b. XTerm app-defaults: backarrowKey=false so xterm sends DEL (0x7f) for
#      backspace, matching the kernel tty's default VERASE (termio.rs: b'\x7f').
#      Without this, xterm defaults to backarrowKey=true → sends ^H, which the
#      line discipline doesn't recognise as erase, so bash sees literal ^H chars.
if [ -f "${CROSS_USR}/lib/X11/app-defaults/XTerm" ]; then
    mkdir -p "${ROOTFS}/usr/lib/X11/app-defaults"
    cp "${CROSS_USR}/lib/X11/app-defaults/XTerm" "${ROOTFS}/usr/lib/X11/app-defaults/XTerm"
fi

# 15. Pack as raw newc cpio (no gzip — see header comment).
if [[ "${NO_PACK}" -eq 1 ]]; then
    echo "assembled rootfs (--no-pack): ${ROOTFS}"
    du -sh "${ROOTFS}"
else
    ( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
    echo "built ${OUTPUT}"
    echo "  systemd: $(file -b "${ROOTFS}/usr/lib/systemd/systemd" | cut -c1-70)"
    echo "  Xorg:    $(file -b "${ROOTFS}/usr/bin/Xorg" | cut -c1-70)"
    echo "  init:    $(file -b "${ROOTFS}/init" | cut -c1-70)"
    du -sh "${ROOTFS}"
    echo "  initramfs: $(du -h "${OUTPUT}" | cut -f1)"
fi
