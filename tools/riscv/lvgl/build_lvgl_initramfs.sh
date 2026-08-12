#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build the LVGL image-display initramfs for the RISC-V QEMU framebuffer chain.
#
# Produces a static riscv64 /init that renders a full-screen image via LVGL on
# /dev/fb0, then packs it as the marker initramfs (with the smoke-test /init)
# for use with tools/riscv/qemu_desktop_boot.py.
#
# Usage:
#   build_lvgl_initramfs.sh [image.png] [output.cpio.gz]
#
# Dependencies: riscv64-linux-gnu-gcc, make, magick (ImageMagick), python3,
# and network access on first run to clone LVGL into target/lvgl.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LVGL_ROOT="${REPO_ROOT}/target/lvgl"
LVGL_SRC="${LVGL_ROOT}/lvgl"
DRV_SRC="${LVGL_ROOT}/lv_drivers"
BUILD_DIR="${LVGL_ROOT}/build"

LVGL_TAG="v8.3.9"
DRV_TAG="v8.3.0"

IMAGE_IN="${1:-${SRC_DIR}/default-image.png}"
OUTPUT="${2:-${REPO_ROOT}/target/qemu-uboot/initramfs-lvgl.cpio.gz}"

# The image must be 1280x1024 (matching the bochs-display resolution).
IMG_W=1280
IMG_H=1024

ensure_lvgl() {
    if [[ ! -d "${LVGL_SRC}/src" ]]; then
        mkdir -p "${LVGL_ROOT}"
        git clone --depth 1 --branch "${LVGL_TAG}" \
            https://github.com/lvgl/lvgl.git "${LVGL_SRC}"
    fi
    if [[ ! -d "${DRV_SRC}/display" ]]; then
        mkdir -p "${LVGL_ROOT}"
        git clone --depth 1 --branch "${DRV_TAG}" \
            https://github.com/lvgl/lv_drivers.git "${DRV_SRC}"
    fi
}

# Patch the vendored LVGL configuration for this target. Idempotent.
patch_lvgl() {
    local lv_conf="${LVGL_SRC}/lv_conf.h"
    local drv_conf="${DRV_SRC}/lv_drv_conf.h"

    # Both config templates wrap their content in `#if 0`; enable it.
    sed -i 's|^#if 0 /\*Set it to "1" to enable the content\*/|#if 1 /*Set it to "1" to enable the content*/|' \
        "${lv_conf}" "${drv_conf}"

    # 32-bit colors to match the x8r8g8b8 framebuffer; resolution caps.
    sed -i 's|^#define LV_COLOR_DEPTH .*|#define LV_COLOR_DEPTH 32|' "${lv_conf}"
    if ! grep -q 'LV_HOR_RES_MAX 1280' "${lv_conf}"; then
        sed -i '0,/^#define LV_COLOR_DEPTH 32/a #define LV_HOR_RES_MAX 1280\n#define LV_VER_RES_MAX 1024' "${lv_conf}"
    fi

    # Enable the fonts the GUI uses (symbols live in montserrat_48).
    for size in 48 32 24 20 16; do
        sed -i "s|^#define LV_FONT_MONTSERRAT_${size} 0|#define LV_FONT_MONTSERRAT_${size} 1|" "${lv_conf}"
    done
    sed -i 's|^#define LV_FONT_DEFAULT .*|#define LV_FONT_DEFAULT \&lv_font_montserrat_20|' "${lv_conf}"

    # Enable the fbdev display and evdev input drivers.
    sed -i 's|^#  define USE_FBDEV           0|#  define USE_FBDEV           1|' "${drv_conf}"
    sed -i 's|^#  define USE_EVDEV           0|#  define USE_EVDEV           1|' "${drv_conf}"

    # fbdev_init aborts on FBIOBLANK failure; efifb-style drivers return
    # EINVAL for FBIOBLANK, so make it non-fatal.
    DRV_FBDEV="${DRV_SRC}/display/fbdev.c" python3 - <<'PYEOF'
import os
from pathlib import Path
p = Path(os.environ["DRV_FBDEV"])
s = p.read_text()
old = "        perror(\"ioctl(FBIOBLANK)\");\n        return;"
new = "        /* Some fbdev drivers (e.g. efifb-style) deliberately do not\n"
new += "         * implement blanking; that must not abort initialization. */\n"
new += "        perror(\"ioctl(FBIOBLANK)\");"
if old in s:
    p.write_text(s.replace(old, new, 1))
PYEOF
}

prepare_image() {
    mkdir -p "${BUILD_DIR}"
    local src="${IMAGE_IN}"
    if [[ ! -f "${src}" ]]; then
        # Generate a default title-card if no image was given.
        src="${BUILD_DIR}/default-image.png"
        magick -size "${IMG_W}x${IMG_H}" gradient:'#1a237e-#000000' \
            -fill '#ff6f00' -font /usr/share/fonts/gnu-free/FreeSansBold.otf -pointsize 96 \
            -gravity center -annotate +0-60 'Asterinas' \
            -fill white -font /usr/share/fonts/gnu-free/FreeSans.otf -pointsize 48 \
            -annotate +0+80 'RISC-V on QEMU' \
            -fill '#90caf9' -pointsize 28 -annotate +0+160 'framebuffer via simple-framebuffer' \
            -fill '#4caf50' -pointsize 28 -annotate +0+210 'LVGL on /dev/fb0' \
            -stroke '#ff6f00' -strokewidth 3 -draw 'line 320,140 960,140' \
            "${src}"
    fi
    local dims
    dims="$(identify -format '%wx%h' "${src}")"
    if [[ "${dims}" != "${IMG_W}x${IMG_H}" ]]; then
        echo "image must be ${IMG_W}x${IMG_H}, got ${dims}" >&2
        exit 2
    fi
    # Convert to raw BGRA8888 (little-endian LV_COLOR_DEPTH 32 layout).
    magick "${src}" -depth 8 -channel-fx 'red<=>blue' rgba:"${BUILD_DIR}/asterinas.bgra"
}

build_init() {
    mkdir -p "${BUILD_DIR}"
    cp "${SRC_DIR}/main.c" "${BUILD_DIR}/main.c"
    local cc="riscv64-linux-gnu-gcc"
    local cflags="-O2 -static -no-pie -fno-stack-protector -DLV_CONF_INCLUDE_SIMPLE"
    cflags+=" -I${LVGL_SRC} -I${LVGL_SRC}/src -I${DRV_SRC} -I${DRV_SRC}/.."
    local lvgl_src
    lvgl_src="$(find "${LVGL_SRC}/src" -name '*.c')"
    local drv_src="${DRV_SRC}/display/fbdev.c ${DRV_SRC}/indev/evdev.c"
    # Compile from BUILD_DIR so the .incbin("asterinas.bgra") resolves.
    # shellcheck disable=SC2086
    (cd "${BUILD_DIR}" && ${cc} ${cflags} -o init main.c ${lvgl_src} ${drv_src})
}

ensure_lvgl
patch_lvgl
prepare_image
build_init

python3 "${REPO_ROOT}/tools/riscv/make_qemu_uboot_initramfs.py" \
    --init-elf "${BUILD_DIR}/init" "${OUTPUT}"

echo "built ${OUTPUT}"
