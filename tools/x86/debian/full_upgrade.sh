#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L6 apt full-upgrade probe: move the guest from trixie to forky.
#
# Runs inside the guest on an image *copy* (the gate never touches the base
# image). Three guest-side stages:
#
#   source : rewrite trixie -> forky in the apt source lists
#   update : apt-get update (with AllowReleaseInfoChange for the suite change)
#   upgrade: apt-get -y full-upgrade, bounded by UPG_UPGRADE_TIMEOUT
#            (default 90 minutes; 40 minutes stopped at 227/341 packages)
#
# The host gate adds stage=copy (before boot) and stage=reboot (a second boot
# of the same image with a login and systemd check).
#
# Console protocol (one `UPG ` line per event):
#
#   UPG env <key>=<value>
#   UPG stage=<source|update|upgrade> rc=<n> dur=<s>
#   UPG disk_free_kb=<n> stage=<name> phase=<before|after>
#   UPG source line=<rewritten sources.list line>
#   UPG update upgradable=<n>
#   UPG upgrade summary=<apt summary line>
#   UPG err=<one-line summary>
#   UPG_DONE
#
# Disk guard: below UPG_MIN_FREE_KB (default 800 MiB) the probe stops before
# the next stage and records `disk`.

MIN_FREE_KB=${UPG_MIN_FREE_KB:-819200}
OLD_SUITE=${UPG_OLD_SUITE:-trixie}
NEW_SUITE=${UPG_NEW_SUITE:-forky}
UPDATE_TIMEOUT=${UPG_UPDATE_TIMEOUT:-600}
UPGRADE_TIMEOUT=${UPG_UPGRADE_TIMEOUT:-5400}
LOGDIR=${UPG_LOGDIR:-/var/tmp/upgrade}

emit() { printf 'UPG %s\n' "$*" >/dev/console; }
sanitize() {
    tr '\n\r\t' '   ' | sed 's/[[:space:]]*$//' |
        tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-220
}
now() { cut -d' ' -f1 /proc/uptime; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }
free_kb() { df -k / 2>/dev/null | awk 'NR==2 { print $4 }'; }

disk_report() {
    emit "disk_free_kb=$(free_kb) stage=$1 phase=$2"
}

err_summary() {
    log=$1
    rc=$2
    msg=$(grep -a -E '^(E:|dpkg: error|Errors were encountered|.*: error:|.*No space left|.*Function not implemented|.*Could not resolve|.*Failed to fetch|.*not installable|.*unmet dependencies)' "$log" 2>/dev/null |
        tail -2 | sanitize)
    [ -n "$msg" ] || msg=$(grep -a -i -E 'error|failed|not installable|unmet|no space' "$log" 2>/dev/null |
        tail -2 | sanitize)
    [ -n "$msg" ] || msg=$(tail -2 "$log" 2>/dev/null | sanitize)
    [ -n "$msg" ] || msg="rc_${rc}_no_log"
    printf '%s' "$msg"
}

mkdir -p "$LOGDIR" 2>/dev/null
emit "env kernel=$(uname -r | sanitize) debian=$(cat /etc/debian_version 2>/dev/null | sanitize)"
emit "env old_suite=$OLD_SUITE new_suite=$NEW_SUITE min_free_kb=$MIN_FREE_KB"
disk_report setup before

stop=0
free=$(free_kb)
if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
    emit "err=disk_low_free_${free}kb"
    stop=1
fi

# ---------------------------------------------------------------- source
if [ "$stop" -eq 0 ]; then
    disk_report source before
    t0=$(now)
    changed=0
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [ -f "$f" ] || continue
        if grep -q "$OLD_SUITE" "$f"; then
            sed -i "s/$OLD_SUITE/$NEW_SUITE/g" "$f"
            changed=$((changed + 1))
        fi
    done
    for f in /etc/apt/sources.list.d/*.sources; do
        [ -f "$f" ] || continue
        if grep -q "$OLD_SUITE" "$f"; then
            sed -i "s/$OLD_SUITE/$NEW_SUITE/g" "$f"
            changed=$((changed + 1))
        fi
    done
    rc=0
    [ "$changed" -gt 0 ] || rc=1
    dur=$(elapsed "$t0" "$(now)")
    emit "stage=source rc=$rc dur=$dur"
    grep -h '^[[:space:]]*deb ' /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null |
        while read -r line; do
            emit "source line=$(printf '%s' "$line" | sanitize)"
        done
    if [ "$rc" -ne 0 ]; then
        emit "err=no_source_list_mentions_$OLD_SUITE"
        stop=1
    fi
    disk_report source after
fi

free=$(free_kb)
if [ "$stop" -eq 0 ] && [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
    emit "err=disk_low_free_${free}kb"
    stop=1
fi

# ---------------------------------------------------------------- update
if [ "$stop" -eq 0 ]; then
    disk_report update before
    t0=$(now)
    timeout -k 10 "$UPDATE_TIMEOUT" env DEBIAN_FRONTEND=noninteractive \
        apt-get update -o Acquire::AllowReleaseInfoChange=true \
        -o Acquire::Retries=2 >"$LOGDIR/update.log" 2>&1 </dev/null
    rc=$?
    dur=$(elapsed "$t0" "$(now)")
    emit "stage=update rc=$rc dur=$dur"
    if [ "$rc" -ne 0 ]; then
        emit "err=$(err_summary "$LOGDIR/update.log" "$rc")"
        stop=1
    else
        upgradable=$(apt list --upgradable 2>/dev/null | grep -c .)
        emit "update upgradable=$upgradable"
    fi
    disk_report update after
fi

free=$(free_kb)
if [ "$stop" -eq 0 ] && [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
    emit "err=disk_low_free_${free}kb"
    stop=1
fi

# ---------------------------------------------------------------- upgrade
if [ "$stop" -eq 0 ]; then
    disk_report upgrade before
    t0=$(now)
    timeout -k 30 "$UPGRADE_TIMEOUT" env DEBIAN_FRONTEND=noninteractive \
        apt-get -y -o Dpkg::Lock::Timeout=300 \
        -o Acquire::AllowReleaseInfoChange=true \
        -o Dpkg::Options::=--force-confold \
        full-upgrade >"$LOGDIR/upgrade.log" 2>&1 </dev/null
    rc=$?
    dur=$(elapsed "$t0" "$(now)")
    emit "stage=upgrade rc=$rc dur=$dur"
    summary=$(grep -a -E '^[0-9]+ upgraded,' "$LOGDIR/upgrade.log" | tail -1 | sanitize)
    [ -n "$summary" ] || summary=$(grep -a -E '^[0-9]+ (upgraded|newly|to remove|not upgraded)' "$LOGDIR/upgrade.log" | tail -1 | sanitize)
    emit "upgrade summary=${summary:-none}"
    if [ "$rc" -ne 0 ]; then
        emit "err=$(err_summary "$LOGDIR/upgrade.log" "$rc")"
        tail -6 "$LOGDIR/upgrade.log" 2>/dev/null | while read -r line; do
            emit "log upgrade=$(printf '%s' "$line" | sanitize)"
        done
    fi
    disk_report upgrade after
fi

# Persist what we can before shutdown; the host reboots this image next.
sync 2>/dev/null
emit "env debian_after=$(cat /etc/debian_version 2>/dev/null | sanitize)"
emit "disk_free_kb=$(free_kb) stage=final phase=after"
printf 'UPG_DONE\n' >/dev/console
