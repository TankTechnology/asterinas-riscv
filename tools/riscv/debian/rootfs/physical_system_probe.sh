#!/bin/sh
# SPDX-License-Identifier: MPL-2.0

set -u

nonce=${1-}
if [ "${#nonce}" -ne 32 ]; then
    exit 2
fi
case "$nonce" in
    *[!0-9a-f]*) exit 2 ;;
esac

emit_probe() {
    token=$1
    value=$2
    status=$3
    prefix="__ASTERINAS_DEBUG_${nonce}_${token}"
    printf '%s_BEGIN__\n%s_VALUE__%s\n%s_STATUS__%s\n%s_END__\n' \
        "$prefix" "$prefix" "$value" "$prefix" "$status" "$prefix"
}

value=$(id -u)
emit_probe UID "$value" "$?"

value=$(tr -d '\n' </proc/1/comm)
emit_probe PID1 "$value" "$?"

value=$(awk '$2 == "/" { print $1, $3; exit }' /proc/mounts)
emit_probe ROOT "$value" "$?"

attempt=0
while ! systemctl is-active --quiet asterinas-desktop-m4-evidence.service &&
    ! systemctl is-active --quiet asterinas-desktop-m5.service; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 45 ] && break
    sleep 1
done
if systemctl is-active --quiet asterinas-desktop-m4-evidence.service ||
    systemctl is-active --quiet asterinas-desktop-m5.service; then
    emit_probe GRAPHICAL active 0
else
    emit_probe GRAPHICAL inactive 1
fi

if systemctl is-active --quiet asterinas-desktop-m4.service ||
    systemctl is-active --quiet asterinas-desktop-m5.service; then
    emit_probe DESKTOP active 0
else
    emit_probe DESKTOP inactive 1
fi
