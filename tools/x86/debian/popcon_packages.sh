#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L1 package-ecosystem probe: install each popcon top package with apt and run
# a smoke check. One line per package on the console so the host can scrape it.
#
# Runs inside the guest; the package list is /usr/local/share/popcon_top50.txt
# (injected by daily_use_gate.sh).

LIST=${POPCON_LIST:-/usr/local/share/popcon_top50.txt}
OUT=${POPCON_OUT:-/tmp/popcon-result.json}
: > "$OUT"

while read -r pkg; do
    [ -n "$pkg" ] || continue
    case "$pkg" in \#*) continue ;; esac

    start=$(cut -d' ' -f1 /proc/uptime)
    if timeout 180 apt-get install -y --no-install-recommends "$pkg" >/tmp/popcon-$pkg.log 2>&1; then
        rc=0
    else
        rc=$?
    fi
    end=$(cut -d' ' -f1 /proc/uptime)
    dur=$(awk -v a="$start" -v b="$end" 'BEGIN{printf "%.2f", b-a}')

    smoke=na
    err=""
    if [ "$rc" -eq 0 ]; then
        if command -v "$pkg" >/dev/null 2>&1; then
            if timeout 10 "$pkg" --version >/dev/null 2>&1 || timeout 10 "$pkg" --help >/dev/null 2>&1; then
                smoke=ok
            else
                smoke=no
                err=$(timeout 10 "$pkg" --version 2>&1 | head -1 | tr ' ' '_' | head -c 60)
            fi
        else
            smoke=lib
        fi
    else
        err=$(tail -2 /tmp/popcon-$pkg.log 2>/dev/null | tr '\n' '|' | tr ' ' '_' | head -c 100)
    fi

    echo "POPCON pkg=$pkg rc=$rc smoke=$smoke dur=$dur" >/dev/console
    printf '{"pkg":"%s","rc":%d,"smoke":"%s","dur":%s,"err":"%s"}\n' \
        "$pkg" "$rc" "$smoke" "$dur" "$err" >> "$OUT"
done < "$LIST"

echo "POPCON_DONE count=$(grep -c pkg= "$OUT" 2>/dev/null)" >/dev/console
