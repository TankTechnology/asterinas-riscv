#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L2 systemd health probe. Runs inside the Debian guest after login and writes
# one self-describing line per data point to /dev/console; the host parses them
# into target/systemd-health/. Every line starts with `SYSD ` except the final
# `SYSD_DONE` marker.
#
# If PID 1 is not systemd (the image fell back to a shell/init script), the
# probe records that fact and stops: it never fabricates measurements.

say() {
    printf 'SYSD %s\n' "$*" >/dev/console
}

# One-line token for key=value fields.
sanitize() {
    tr '\n\r\t' '   ' | tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-200
}

# Display text (critical chain): keep spaces and dots, drop box drawing.
disp() {
    tr '\n\r\t' '   ' | tr -c 'A-Za-z0-9 ._=+:/@,-' '_' | cut -c1-200
}

if [ ! -d /run/systemd/system ]; then
    pid1=$(cat /proc/1/comm 2>/dev/null | sanitize)
    say "systemd=no pid1=$pid1"
    printf 'SYSD_DONE systemd=no\n' >/dev/console
    exit 0
fi

say "systemd=yes pid1=$(cat /proc/1/comm 2>/dev/null | tr -d '\r\n' | sanitize)"
say "version=$(systemctl --version 2>/dev/null | head -1 | tr -d '\r\n' | sanitize)"
say "is_system_running=$(systemctl is-system-running 2>/dev/null | head -1 | tr -d '\r\n' | sanitize)"
say "uptime=$(cut -d' ' -f1 /proc/uptime)"

# --- startup time: firmware / loader / kernel / userspace (+ initrd if any) --
line=$(systemd-analyze 2>&1 | head -1)
# Startup phases print as "322ms (kernel)" or "3.010s (userspace)".
phase() {
    v=$(printf '%s\n' "$line" | sed -n "s/.* \([0-9.]*\)s ($1).*/\1/p")
    if [ -n "$v" ]; then
        echo "$v"
        return
    fi
    v=$(printf '%s\n' "$line" | sed -n "s/.* \([0-9.]*\)ms ($1).*/\1/p")
    [ -n "$v" ] && awk -v x="$v" 'BEGIN{printf "%.3f", x/1000}'
}
fw=$(phase firmware); [ -n "$fw" ] || fw=0.00
ld=$(phase loader);   [ -n "$ld" ] || ld=0.00
kern=$(phase kernel); [ -n "$kern" ] || kern=0.00
initrd=$(phase initrd); [ -n "$initrd" ] || initrd=0.00
user=$(phase userspace); [ -n "$user" ] || user=0.00
total=$(printf '%s\n' "$line" | sed -n 's/.*= *\([0-9.]*\)s.*/\1/p')
[ -n "$total" ] || total=0.00
say "analyze firmware=$fw loader=$ld kernel=$kern initrd=$initrd userspace=$user total=$total"
say "analyze_raw=$(printf '%s' "$line" | sanitize)"

# --- slowest 20 units ------------------------------------------------------
# Time may be "1min 5.849s", "620ms" or "29.144s"; normalise all three.
systemd-analyze blame --no-pager 2>/dev/null | head -20 | awk '{
    if ($1 ~ /min$/) {
        t=$1; sub(/min$/,"",t); s=$2; sub(/s$/,"",s); sec=t*60+s; u=$3
    } else if ($1 ~ /ms$/) {
        t=$1; sub(/ms$/,"",t); sec=t/1000; u=$2
    } else {
        t=$1; sub(/s$/,"",t); sec=t; u=$2
    }
    printf "SYSD blame unit=%s sec=%.3f\n", u, sec
}' >/dev/console

# --- critical chain (first 15 lines) ---------------------------------------
systemd-analyze critical-chain --no-pager 2>/dev/null | head -15 | nl -ba |
    while read -r n rest; do
        say "critical rank=$n line=$(printf '%s' "$rest" | disp)"
    done

# --- verify three key units ------------------------------------------------
for unit in multi-user.target getty@.service ssh.service; do
    if systemctl list-unit-files --no-legend --plain -- "$unit" 2>/dev/null |
        grep -q "^$unit"; then
        out=$(timeout 60 systemd-analyze verify "$unit" 2>&1)
        rc=$?
        say "verify unit=$unit rc=$rc"
        [ -n "$out" ] &&
            say "verify_msg unit=$unit msg=$(printf '%s' "$out" | head -3 | sanitize)"
    else
        say "verify unit=$unit rc=skip"
    fi
done

# --- counts ----------------------------------------------------------------
running=$(systemctl list-units --type=service --state=running \
    --no-pager --plain --no-legend 2>/dev/null | grep -c .)
units_total=$(systemctl list-units --all \
    --no-pager --plain --no-legend 2>/dev/null | grep -c .)
unit_files=$(systemctl list-unit-files \
    --no-pager --plain --no-legend 2>/dev/null | grep -c .)
say "running_services=$running"
say "units_total=$units_total"
say "unit_files=$unit_files"

# --- failed units, with show state and one journal line each ---------------
failed_list=$(systemctl --failed --no-pager --plain --no-legend 2>/dev/null)
failed_count=$(printf '%s\n' "$failed_list" | grep -c .)
say "failed_count=$failed_count"
printf '%s\n' "$failed_list" | while read -r unit load active sub desc; do
    [ -n "$unit" ] || continue
    say "failed unit=$unit load=$load active=$active sub=$sub desc=$(printf '%s' "$desc" | sanitize)"
    res=$(systemctl show -p Result -p ExecMainStatus --value "$unit" 2>/dev/null |
        paste -sd/ -)
    say "failed_result unit=$unit res=$(printf '%s' "$res" | sanitize)"
    ev=$(journalctl -b -u "$unit" --no-pager -o cat -n 3 2>/dev/null | sanitize)
    [ -n "$ev" ] ||
        ev=$(systemctl status --no-pager -n 3 "$unit" 2>&1 | tail -3 | sanitize)
    say "failed_evid unit=$unit ev=$ev"
done

# --- this boot's error-level journal ---------------------------------------
jerr_count=$(journalctl -p err -b --no-pager 2>/dev/null | tail -n +2 | wc -l)
say "jerr_count=$jerr_count"
journalctl -p err -b --no-pager -o cat 2>/dev/null | head -20 |
    while read -r msg; do
        [ -n "$msg" ] || continue
        say "jerr msg=$(printf '%s' "$msg" | sanitize)"
    done

printf 'SYSD_DONE systemd=yes failed=%s running=%s units=%s jerr=%s\n' \
    "$failed_count" "$running" "$units_total" "$jerr_count" >/dev/console
