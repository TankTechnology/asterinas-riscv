#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Record how long the system has been up, often enough and durably enough to
# survive a watchdog reset.
#
# The board's failure mode is a kernel that stops without a word: journald does
# not get to flush its tail on a hardware reset, so the last records of the
# boot that died are usually missing. A file that is rewritten and synced every
# few seconds survives, and its value says exactly when the kernel stopped
# making progress -- compare it with `journalctl -b -1 -o short-monotonic`.

target="${ASTERINAS_HEARTBEAT_FILE:-/var/log/asterinas-heartbeat}"
interval="${ASTERINAS_HEARTBEAT_INTERVAL:-5}"

while :; do
    printf 'uptime=%s\n' "$(cut -d' ' -f1 </proc/uptime)" >"$target.tmp" 2>/dev/null &&
        mv -f "$target.tmp" "$target" 2>/dev/null &&
        sync
    sleep "$interval"
done
