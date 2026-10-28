#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# BROWSER-M10 render-matrix harness.
#
# Unlike render_matrix.sh (M6) — which re-ran the full build_systemd_desktop.sh
# per site — this harness builds the base rootfs ONCE and then produces each
# site's initramfs by only rewriting /etc/netsurf.conf and re-packing the raw
# newc cpio (the cpio re-pack is ~0.2 s, so per-site initramfs cost is negligible
# vs the ~2-3 min full rebuild). Each site is then booted and fetch-scored by
# net_validate.sh (the M8 kernel-net harness: re-pack boot disk + boot + grep the
# NetSurf curl outcome), and the framebuffer screenshot is pixel-validated by
# pixel_validate.py (rendered vs empty-root vs black).
#
# Usage:
#   tools/riscv/xorg/render_matrix_net.sh                 # full matrix
#   tools/riscv/xorg/render_matrix_net.sh iana textnpr    # subset (site names)
#   PARALLEL=2 SETTLE=240 tools/riscv/xorg/render_matrix_net.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/systemd"
XORG_DIR="${REPO_ROOT}/tools/riscv/xorg"
BUILD_SCRIPT="${SRC_DIR}/build_systemd_desktop.sh"
NET_VALIDATE="${XORG_DIR}/net_validate.sh"
PIXEL_VALIDATE="${XORG_DIR}/pixel_validate.py"
ROOTFS="${REPO_ROOT}/target/systemd-desktop/rootfs"

OUT_DIR="${OUT_DIR:-/tmp/browser-m10}"
PARALLEL="${PARALLEL:-2}"
SETTLE="${SETTLE:-240}"
LOGLEVEL="${LOGLEVEL:-warn}"

# name|url — one entry per page archetype. M10 adds a set of static-friendly real
# sites (text-only news, org project homepages, static documentation) on top of the
# M6 set, targeting hosts that serve plain HTML over HTTPS with minimal/no JS so
# NetSurf's curl fetcher can actually pull them through the guest network stack.
#
# NOTE: wikipedia / hackernews / lite.duckduckgo.com are intentionally NOT listed:
# they time out from *this host's* network (both IPv4 and IPv6), so a guest boot
# against them always reads code7 for a reason unrelated to the kernel or slirp
# (see BROWSER-M10-report.md §3.4). The wiki/search/news archetypes are covered by
# wiki.archlinux.org / text.npr.org / lite.cnn.com instead.
ALL_SITES=(
  "home|file:///usr/share/netsurf/netsurf-home.html"
  "giftest|file:///usr/share/netsurf/netsurf-giftest.html"
  "iana|https://www.iana.org/"
  "example|https://example.com/"
  "rfc|https://www.rfc-editor.org/rfc/rfc768.html"
  "cnnlite|https://lite.cnn.com/"
  "textnpr|https://text.npr.org/"
  "kernel|https://www.kernel.org/"
  "ietf|https://www.ietf.org/"
  "gnu|https://www.gnu.org/"
  "openbsd|https://www.openbsd.org/"
  "debian|https://www.debian.org/"
  "freebsd|https://www.freebsd.org/"
  "wikiarchlinux|https://wiki.archlinux.org/"
  "w3|https://www.w3.org/"
  "suckless|https://suckless.org/"
  "openwall|https://www.openwall.com/"
)

die() { echo "render_matrix_net: $*" >&2; exit 2; }

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
[ -x "$BUILD_SCRIPT" ] || die "missing $BUILD_SCRIPT"
[ -x "$NET_VALIDATE" ] || die "missing $NET_VALIDATE"
[ -x "$PIXEL_VALIDATE" ] || die "missing $PIXEL_VALIDATE"

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR/results.txt"

# 1. Build the base rootfs once (no /etc/netsurf.conf — netsurf.service then falls
#    back to its bundled local home page). Fresh every run so a stale per-site
#    /etc/netsurf.conf can never leak into the next site (the same staleness class
#    that caused the BROWSER-M9 crash-loop).
echo "[base] assembling base rootfs (--no-pack)"
NETSURF_URL= bash "$BUILD_SCRIPT" --no-pack > "$OUT_DIR/base-build.log" 2>&1 \
    || die "base rootfs build failed (see $OUT_DIR/base-build.log)"

prepare_site() {
    local name="$1" url="$2" dir initrd
    dir="$OUT_DIR/$name"
    initrd="$dir/initramfs.cpio"
    mkdir -p "$dir"
    printf 'NETSURF_URL=%s\n' "$url" > "$ROOTFS/etc/netsurf.conf"
    # No 2>/dev/null here: a full /tmp makes cpio emit "No space left on device"
    # and truncate the initramfs, which is exactly the failure we must not hide.
    ( cd "$ROOTFS" && find . | cpio -o -H newc > "$initrd" )
    local bytes; bytes="$(wc -c < "$initrd")"
    if [ "$bytes" -lt 1000000 ]; then
        echo "[prepare] $name FAILED: initramfs is $bytes bytes (cpio failed — /tmp full?)" >&2
        return 1
    fi
    echo "[prepare] $name <- $url ($bytes bytes)"
}

boot_site() {
    local name="$1"
    local dir="$OUT_DIR/$name"
    echo "[boot] $name starting"
    # net_validate.sh re-packs an independent boot disk (current kernel + this
    # initramfs), boots one guest, and appends "<name> <fetch-outcome>" to
    # "$OUT_DIR/results.txt". The per-name mon.sock / serial.log / shot.ppm land in
    # "$dir" so PARALLEL guests never collide.
    OUT_DIR="$OUT_DIR" bash "$NET_VALIDATE" "$name" "$dir/initramfs.cpio" "$SETTLE" "$LOGLEVEL" \
        > "$dir/net_validate.out" 2>&1
    # Reclaim the per-site boot disk (~160 MB) the moment the boot is done; the
    # serial.log + shot.ppm are the only artifacts the matrix keeps. Without this
    # the 17-site matrix would exceed the 7.7 GB /tmp tmpfs mid-run.
    rm -f "$dir/boot.ext4" "$dir/mon.sock"
    if [ -s "$dir/shot.ppm" ]; then
        python3 "$PIXEL_VALIDATE" "$dir/shot.ppm" > "$dir/pixel.out" 2>&1
        echo "[pixel] $name: $(cat "$dir/pixel.out")"
    else
        echo "[pixel] $name: no screenshot captured"
    fi
    echo "[boot] $name done"
}

echo "=== BROWSER-M10 render matrix: ${#SITES[@]} site(s) ==="

# 2. Prepare all site initramfs sequentially (each is a ~0.2 s conf-rewrite + cpio
#    re-pack; the ROOTFS dir is shared, so this must not run in parallel).
for entry in "${SITES[@]}"; do
    name="${entry%%|*}"; url="${entry#*|}"
    prepare_site "$name" "$url" || die "prepare failed for $name"
done

# 3. Boot + score in parallel batches (each guest is 2 GiB; cap by PARALLEL).
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
echo "=== BROWSER-M10 render matrix results ==="
if [ -s "$OUT_DIR/results.txt" ]; then
    cat "$OUT_DIR/results.txt"
fi
for entry in "${SITES[@]}"; do
    name="${entry%%|*}"
    pix="$(cat "$OUT_DIR/$name/pixel.out" 2>/dev/null || echo 'n/a')"
    printf "  %-14s %s\n" "$name" "$pix"
done
echo "Each dir: serial.log (systemd+NetSurf log), shot.ppm (framebuffer), pixel.out, net_validate.out."
