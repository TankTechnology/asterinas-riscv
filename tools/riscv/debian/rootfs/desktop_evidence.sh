#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Collect what "the desktop is up" looks like from inside the guest.
#
# The QEMU reference and the board differ in hardware, so comparing screens is
# not enough: this prints the same facts on both, so a board that shows a
# cursor on a black background can be diffed against a QEMU guest that shows a
# full desktop instead of guessed at.
#
# Run it from a root shell (the isolated debug console) and keep the output.

echo "=== processes ==="
for name in Xorg openbox pcmanfm lxpanel firefox; do
    line=$(pgrep -a "$name" 2>/dev/null | head -n 1)
    printf '%-10s %s\n' "$name" "${line:-missing}"
done

echo "=== units ==="
for unit in asterinas-desktop-m5 asterinas-desktop-ready asterinas-browser-web; do
    printf '%-26s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null)"
done

echo "=== readiness ==="
if [ -f /run/asterinas-desktop-ready ]; then
    echo "marker: $(cat /run/asterinas-desktop-ready)"
else
    echo "marker: missing"
fi

echo "=== devices ==="
ls -l /dev/fb0 2>&1 | sed 's/^/  /'
ls -l /dev/input/event* 2>&1 | sed 's/^/  /'

echo "=== framebuffer ==="
# A live desktop leaves non-zero pixels in the visible area; a black screen
# reads back as zeros even though Xorg is running.
if [ -c /dev/fb0 ]; then
    dd if=/dev/fb0 bs=4096 count=8 2>/dev/null |
        od -An -tu1 -v |
        awk '{ for (i = 1; i <= NF; i++) { n++; if ($i != 0) nz++ } }
             END { printf "  sampled=%d nonzero=%d\n", n, nz }'
else
    echo "  no /dev/fb0"
fi

echo "=== kernel/marker log ==="
journalctl -b -k --no-pager 2>/dev/null |
    grep -a 'ASTERINAS_DESKTOP' | tail -n 6 | sed 's/^/  /'

echo "=== gate stages ==="
journalctl -b -u asterinas-desktop-ready --no-pager 2>/dev/null |
    grep -a 'PHYSICAL_BROWSER_STAGE\|status=' | tail -n 8 | sed 's/^/  /'

echo "=== xorg log tail ==="
tail -n 12 /home/asterinas/Xorg.0.log 2>/dev/null | sed 's/^/  /'
