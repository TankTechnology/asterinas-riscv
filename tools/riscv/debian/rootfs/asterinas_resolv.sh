#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Point the resolver at servers that exist on the board's network.
#
# The image ships the QEMU slirp resolver (10.0.2.3) because that is what the
# emulated gates use. On the board that address does not exist, and every name
# lookup fails: no DNS means no certificate check, no clock source, and a
# browser that can only load pages by address. The kernel command line says
# which network we are on, so the switch is made there rather than at build
# time -- the same image still works in QEMU.

set -u

cmdline="$(cat /proc/cmdline 2>/dev/null || true)"
case " $cmdline " in
*" asterinas.net="*) ;;
*) exit 0 ;;
esac

resolv=/etc/resolv.conf
# Keep a hand-written resolver that is not the slirp default: whoever edited
# it knew something about this network that we do not.
if [ -r "$resolv" ] && ! grep -q '^nameserver 10\.0\.2\.3$' "$resolv"; then
    exit 0
fi

tmp="$resolv.asterinas"
{
    echo "# Written by asterinas-resolv: the board is not on QEMU slirp."
    echo "nameserver 114.114.114.114"
    echo "nameserver 223.5.5.5"
} >"$tmp" 2>/dev/null || exit 0
mv -f "$tmp" "$resolv" 2>/dev/null || rm -f "$tmp"
printf 'ASTERINAS_RESOLV_SET servers=114.114.114.114,223.5.5.5\n'
