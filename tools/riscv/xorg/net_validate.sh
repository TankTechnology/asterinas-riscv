#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# BROWSER-M8 network-flakiness validator.
#
# Re-packs a boot disk from an existing (already-built) initramfs + the *current*
# kernel Image, boots it with slirp networking, and reports the NetSurf fetch
# outcome (code 7 / code 56 / HTTP 200) from the serial log. This isolates the
# kernel-net fix from the slow initramfs rebuild: only the kernel changes between
# runs, so re-packing is the only per-run cost.
#
# Usage:
#   tools/riscv/xorg/net_validate.sh <name> <initramfs.cpio> [settle_seconds] [extra...]
# e.g.
#   tools/riscv/xorg/net_validate.sh example /tmp/browser-m7/example/initramfs.cpio 150

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

BOOT_DRIVER="${REPO_ROOT}/tools/riscv/systemd/boot_systemd_desktop.py"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
UBOOT="${REPO_ROOT}/target/qemu-uboot/cache/u-boot-build/u-boot"
OUT_DIR="${OUT_DIR:-/tmp/browser-m8}"

name="${1:?usage: net_validate.sh <name> <initramfs.cpio> [settle] [log-level]}"
initrd="${2:?missing initramfs path}"
settle="${3:-150}"
loglevel="${4:-warn}"

[ -s "$KERNEL_IMAGE" ] || { echo "missing kernel Image" >&2; exit 2; }
[ -s "$UBOOT" ] || { echo "missing U-Boot" >&2; exit 2; }
[ -s "$DTB" ] || { echo "missing DTB" >&2; exit 2; }
[ -s "$initrd" ] || { echo "missing initramfs: $initrd" >&2; exit 2; }

dir="${OUT_DIR}/${name}"
mkdir -p "$dir"

# Re-pack an independent boot disk with the current kernel (fast; initramfs unchanged).
stage="$(mktemp -d)"
cp "$KERNEL_IMAGE" "$stage/asterinas.booti"
cp "$initrd" "$stage/initramfs.cpio.gz"
cp "$DTB" "$stage/qemu-virt.dtb"
initrd_bytes=$(wc -c < "$initrd")
kernel_bytes=$(wc -c < "$KERNEL_IMAGE")
boot_mb=$(( (initrd_bytes + kernel_bytes + 64*1024*1024)/1024/1024 + 1 ))
(( boot_mb < 128 )) && boot_mb=128
truncate -s "${boot_mb}M" "$dir/boot.ext4"
mkfs.ext4 -q -F -d "$stage" "$dir/boot.ext4"
rm -rf "$stage"

log="$dir/serial.log" sock="$dir/mon.sock" ppm="$dir/shot.ppm"
rm -f "$log" "$sock" "$ppm"

python3 "$BOOT_DRIVER" \
    --boot-disk "$dir/boot.ext4" --net \
    --mon-sock "$sock" --serial-log "$log" \
    --screenshot "$ppm" \
    --settle-seconds "$settle" \
    --loglevel "$loglevel" \
    > "$dir/boot.out" 2>&1

# Summarize the network outcome from the NetSurf serial log, scoring the *main*
# page's fetch rather than any secondary resource. NetSurf fetches a default
# `http://www.google.com/favicon.ico` for any page without a favicon, and on
# hosts where google.com is unreachable that fetch code7s and would otherwise
# mask a successful main-page http200 (BROWSER-M10 §3.4). So: for a remote page,
# an HTTP 200 from the main fetch wins over a later favicon code7; for a local
# `file://` page, "rendered" is the only meaningful outcome.
nav_url="$(grep -ao 'browser_window_navigate: bw [^,]*, url [^ ]*' "$log" | head -1 | sed -E 's/.* url //')"
if [ -z "$nav_url" ]; then
    res="unknown"
elif [[ "$nav_url" == file://* ]]; then
    if grep -qa 'content_scaled_redraw' "$log"; then res="redraw"; else res="unknown"; fi
else
    if   grep -qa 'HTTP status code 200' "$log"; then res="http200"
    elif grep -qa 'Unknown cURL response code 56' "$log"; then res="code56"
    elif grep -qa 'Unknown cURL response code 28' "$log"; then res="code28"
    elif grep -qa 'Unknown cURL response code 6' "$log"; then res="code6"
    elif grep -qa 'Unknown cURL response code 7' "$log"; then res="code7"
    elif grep -qa 'content_scaled_redraw' "$log"; then res="redraw"
    else res="unknown"; fi
fi

nav=$(grep -qa 'browser_window_navigate' "$log" && echo yes || echo no)
box=$(grep -qa 'html_box_convert_done' "$log" && echo yes || echo no)
echo "[result] $name: fetch=$res nav=$nav box=$box"
echo "$name $res" >> "$OUT_DIR/results.txt"
