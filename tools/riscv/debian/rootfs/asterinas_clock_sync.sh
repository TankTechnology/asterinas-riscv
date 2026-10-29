#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Set the clock from an HTTP `Date:` header.
#
# The Megrez has no battery-backed clock with a usable value: it powers up at
# 2026-04-13 every time. Everything that validates certificates then fails —
# Firefox shows "Your Computer Clock is Wrong", curl rejects every HTTPS site
# with "certificate is not yet valid".
#
# The campus gateway answers HTTP even before the portal login (the response is
# the portal redirect page), so its `Date:` header is the one time source that
# is available on a cold boot with no credentials. If that is not reachable the
# script leaves the clock alone and exits successfully: a wrong clock is bad,
# but a unit that fails would take the desktop session down with it.

set -u

gateway=""
if [ -r /proc/cmdline ]; then
    gateway="$(sed -n 's/.*asterinas\.net=[^,]*,\([0-9.]*\)\/[0-9]*,\([0-9.]*\).*/\2/p' /proc/cmdline)"
fi
[ -n "$gateway" ] || gateway="58.206.232.1"

for url in "http://${gateway}/" "http://auth.zgclab.edu.cn/" "https://www.baidu.com/"; do
    header="$(curl -m 6 -sI "$url" 2>/dev/null | sed -n 's/^[Dd]ate:[[:space:]]*//p' | head -n 1 | tr -d '\r')"
    [ -n "$header" ] || continue
    if date -u -s "$header" >/dev/null 2>&1; then
        echo "ASTERINAS_CLOCK_SYNC source=$url date=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        exit 0
    fi
done

echo "ASTERINAS_CLOCK_SYNC source=none date=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
exit 0
