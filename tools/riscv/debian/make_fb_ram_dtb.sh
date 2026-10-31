#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build a RAM-backed simple-framebuffer DTB from the QEMU bochs DTB.
#
# The direct-boot guest cannot program the bochs PCI BAR: no firmware runs
# before Asterinas, so the BAR stays unassigned and the DTB address at
# 0x40000000 is a hole.  Move the framebuffer into the top of QEMU RAM and
# shrink the memory node so the kernel keeps the region out of the allocator.
# The host then reads the pixels with the QEMU monitor's pmemsave.

set -euo pipefail
umask 077

readonly FRAMEBUFFER_SIZE=$((0x1000000))
readonly MEMORY_RESERVE=$((0x10000000))

die() {
    printf 'make_fb_ram_dtb: %s\n' "$*" >&2
    exit 2
}

if [[ $# != 2 ]]; then
    die "usage: $0 <qemu-bochs.dtb> <fb-ram.dtb>"
fi
readonly BASE=$1
readonly OUTPUT=$2

[[ -f "$BASE" ]] || die "base DTB not found: $BASE"
command -v fdtget >/dev/null 2>&1 || die "fdtget is required (device-tree-compiler)"
command -v fdtput >/dev/null 2>&1 || die "fdtput is required (device-tree-compiler)"

mkdir -p -- "$(dirname -- "$OUTPUT")"
temporary="${OUTPUT}.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT
cp -- "$BASE" "$temporary"

read -r -a cells < <(fdtget -t x "$temporary" /memory@80000000 reg)
[[ ${#cells[@]} == 4 ]] || die "memory@80000000 does not have four reg cells"
memory_high=$((16#${cells[0]}))
memory_low=$((16#${cells[1]}))
size_high=$((16#${cells[2]}))
size_low=$((16#${cells[3]}))
(( memory_high == 0 && size_high == 0 )) ||
    die "only a 32-bit QEMU virt memory window is supported"
(( size_low > MEMORY_RESERVE + FRAMEBUFFER_SIZE )) ||
    die "base memory is too small for the reserved framebuffer"

new_size=$((size_low - MEMORY_RESERVE))
framebuffer_address=$((memory_low + new_size))
(( framebuffer_address >= memory_low + new_size )) ||
    die "framebuffer does not start above the usable memory node"
(( framebuffer_address + FRAMEBUFFER_SIZE <= memory_low + size_low )) ||
    die "framebuffer does not fit inside the original memory window"

fdtput -t x "$temporary" /memory@80000000 reg \
    0x0 "$(printf '0x%x' "$memory_low")" 0x0 "$(printf '0x%x' "$new_size")"
fdtput -t x "$temporary" /framebuffer@40000000 reg \
    0x0 "$(printf '0x%x' "$framebuffer_address")" 0x0 "$(printf '0x%x' "$FRAMEBUFFER_SIZE")"

read -r -a memory_result < <(fdtget -t x "$temporary" /memory@80000000 reg)
read -r -a framebuffer_result < <(fdtget -t x "$temporary" /framebuffer@40000000 reg)
[[ "${memory_result[*]}" == "0 $(printf '%x' "$memory_low") 0 $(printf '%x' "$new_size")" ]] ||
    die "memory reg verification failed"
[[ "${framebuffer_result[*]}" == "0 $(printf '%x' "$framebuffer_address") 0 $(printf '%x' "$FRAMEBUFFER_SIZE")" ]] ||
    die "framebuffer reg verification failed"

width=$(fdtget -t x "$temporary" /framebuffer@40000000 width)
height=$(fdtget -t x "$temporary" /framebuffer@40000000 height)
stride=$(fdtget -t x "$temporary" /framebuffer@40000000 stride)
mv -f -- "$temporary" "$OUTPUT"
trap - EXIT
printf 'FB_RAM_DTB output=%s address=0x%x width=0x%s height=0x%s stride=0x%s\n' \
    "$OUTPUT" "$framebuffer_address" "$width" "$height" "$stride"
