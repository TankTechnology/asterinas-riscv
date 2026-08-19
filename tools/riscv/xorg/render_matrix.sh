#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# BROWSER-M5 render-quality matrix: boot the systemd desktop (NetSurf) against a
# set of URLs covering distinct page archetypes and collect a screenshot + serial
# log per site. Each site gets its own initramfs (NETSURF_URL baked into
# /etc/netsurf.conf) and boot disk under /tmp/browser-m5/<name>/, then boots on
# an independent QEMU instance so the shared VNC guest is left untouched.
#
# The kernel is NOT rebuilt; it must already exist at
#   target/osdk/aster-kernel-osdk-bin.Image  (override with ASTERINAS_RISCV_BOOTI=).
#
# Usage:
#   tools/riscv/xorg/render_matrix.sh                 # full matrix
#   tools/riscv/xorg/render_matrix.sh iana wikipedia  # subset (site names)
#   PARALLEL=3 SETTLE=180 tools/riscv/xorg/render_matrix.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/systemd"
BUILD_SCRIPT="${SRC_DIR}/build_systemd_desktop.sh"
BOOT_DRIVER="${SRC_DIR}/boot_systemd_desktop.py"
KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
UBOOT="${REPO_ROOT}/target/qemu-uboot/cache/u-boot-build/u-boot"
OUT_DIR="${OUT_DIR:-/tmp/browser-m6}"
PARALLEL="${PARALLEL:-3}"
SETTLE="${SETTLE:-300}"
COLLECT="${COLLECT:-300}"
RETRY="${RETRY:-1}"

# name|url  — one entry per page archetype. M6 adds the image-test page (local,
# deterministic) plus doc and news archetypes on top of the M5 set.
ALL_SITES=(
  "home|file:///usr/share/netsurf/netsurf-home.html"
  "imagetest|file:///usr/share/netsurf/netsurf-imagetest.html"
  "giftest|file:///usr/share/netsurf/netsurf-giftest.html"
  "iana|https://www.iana.org/"
  "infocern|https://info.cern.ch/"
  "hackernews|https://news.ycombinator.com/"
  "wikipedia|https://en.wikipedia.org/wiki/RISC-V"
  "example|https://example.com/"
  "csszengarden|https://www.csszengarden.com/"
  "man7|https://man7.org/linux/man-pages/man2/open.2.html"
  "rfc|https://www.rfc-editor.org/rfc/rfc768.html"
  "cnnlite|https://lite.cnn.com/"
)

die() { echo "render_matrix: $*" >&2; exit 2; }

# Select the site subset (by name) or fall back to all.
declare -a SITES
if [ "$#" -gt 0 ]; then
    for want in "$@"; do
        for entry in "${ALL_SITES[@]}"; do
            name="${entry%%|*}"
            [ "$name" = "$want" ] && SITES+=("$entry")
        done
    done
else
    SITES=("${ALL_SITES[@]}")
fi
[ ${#SITES[@]} -gt 0 ] || die "no matching sites"

command -v qemu-system-riscv64 >/dev/null || die "qemu-system-riscv64 not found"
[ -s "$KERNEL_IMAGE" ] || die "missing kernel Image (set ASTERINAS_RISCV_BOOTI=)"
[ -s "$UBOOT" ] || die "missing U-Boot"
[ -s "$DTB" ] || die "missing DTB"

mkdir -p "$OUT_DIR"

prepare_site() {
    local name="$1" url="$2"
    local dir="$OUT_DIR/$name" initrd="$OUT_DIR/$name/initramfs.cpio" disk="$OUT_DIR/$name/boot.ext4"
    mkdir -p "$dir"
    echo "[prepare] $name <- $url"
    # Build a per-site initramfs (NETSURF_URL baked into /etc/netsurf.conf).
    NETSURF_URL="$url" bash "$BUILD_SCRIPT" "$initrd" > "$dir/build.log" 2>&1 \
        || { echo "[prepare] $name BUILD FAILED"; return 1; }
    # Re-pack an independent boot disk (kernel + initramfs + DTB).
    local stage; stage="$(mktemp -d)"
    cp "$KERNEL_IMAGE" "$stage/asterinas.booti"
    cp "$initrd" "$stage/initramfs.cpio.gz"
    cp "$DTB" "$stage/qemu-virt.dtb"
    local initrd_bytes kernel_bytes boot_mb
    initrd_bytes=$(wc -c < "$initrd")
    kernel_bytes=$(wc -c < "$KERNEL_IMAGE")
    boot_mb=$(( (initrd_bytes + kernel_bytes + 64*1024*1024)/1024/1024 + 1 ))
    (( boot_mb < 128 )) && boot_mb=128
    truncate -s "${boot_mb}M" "$disk"
    mkfs.ext4 -q -F -d "$stage" "$disk"
    rm -rf "$stage"
    echo "[prepare] $name done ($boot_mb MiB disk)"
}

boot_site() {
    local name="$1"
    local dir="$OUT_DIR/$name"
    local disk="$dir/boot.ext4" sock="$dir/mon.sock" log="$dir/serial.log"
    local ppm="$dir/shot.ppm" png="$dir/shot.png"
    echo "[boot] $name starting"
    # The 91 MB raw-cpio initramfs unpack in the kernel can be non-deterministically
    # slow (M5 §3.2), and NetSurf's own startup can stall (M1). Retry a boot that
    # never reached userspace or never navigated NetSurf to a page.
    for attempt in $(seq 1 $((RETRY + 1))); do
        rm -f "$sock" "$ppm" "$log" "$png"
        python3 "$BOOT_DRIVER" \
            --boot-disk "$disk" --net \
            --mon-sock "$sock" \
            --serial-log "$log" \
            --screenshot "$ppm" \
            --settle-seconds "$SETTLE" \
            --collect-timeout "$COLLECT" \
            > "$dir/boot.out" 2>&1
        if grep -qa 'rootfs is ready' "$log" 2>/dev/null \
           && grep -qa 'browser_window_navigate' "$log" 2>/dev/null; then
            echo "[boot] $name: reached userspace + navigated (attempt $attempt)"
            break
        fi
        echo "[boot] $name: attempt $attempt did not navigate; retrying"
    done
    if [ -s "$ppm" ]; then
        if command -v magick >/dev/null 2>&1; then magick "$ppm" "$png"
        elif command -v convert >/dev/null 2>&1; then convert "$ppm" "$png"; fi
    fi
    echo "[boot] $name done"
}

echo "=== BROWSER-M6 render matrix: ${#SITES[@]} site(s) ==="

# Prepare all sites sequentially (fast; the build script shares one rootfs dir).
for entry in "${SITES[@]}"; do
    name="${entry%%|*}"; url="${entry#*|}"
    prepare_site "$name" "$url" || die "prepare failed for $name"
done

# Boot in parallel batches (each guest is 2 GiB; cap by PARALLEL).
running=0
for entry in "${SITES[@]}"; do
    name="${entry%%|*}"
    boot_site "$name" &
    running=$((running+1))
    if [ "$running" -ge "$PARALLEL" ]; then
        wait -n 2>/dev/null || true
        running=$((running-1))
    fi
done
wait

echo ""
echo "=== BROWSER-M5 render matrix artifacts ==="
for entry in "${SITES[@]}"; do
    name="${entry%%|*}"
    printf "  %-14s %s\n" "$name" "$OUT_DIR/$name/"
done
echo "Each dir: shot.png (screenshot), serial.log (systemd+NetSurf log), boot.out."
