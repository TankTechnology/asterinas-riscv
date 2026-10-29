#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# A fetch tool that names the kernel honestly.
#
# `uname` deliberately reports Linux-compatible values (sysname "Linux",
# release 5.13.0) because glibc and most software inspect those fields, so no
# stock fetch program can tell that Asterinas is underneath. This one reads the
# ASTERINAS_* fields the image puts in /etc/os-release and says so explicitly.

set -u

read_field() {
    # read_field <os-release key> <fallback>
    key="$1"
    fallback="$2"
    if [ -r /etc/os-release ]; then
        value="$(sed -n "s/^${key}=\"\{0,1\}\([^\"]*\)\"\{0,1\}$/\1/p" /etc/os-release | head -n 1)"
        [ -n "$value" ] && { printf '%s' "$value"; return; }
    fi
    printf '%s' "$fallback"
}

pretty="$(read_field PRETTY_NAME 'Asterinas')"
kernel="$(uname -r)"
machine="$(uname -m)"
hostname_value="$(uname -n)"

cpu="$(awk -F': ' '/^[Cc]pu[0-9]+[[:space:]]*:|model name|Processor/ { if ($2 != "") { print $2; exit } }' /proc/cpuinfo 2>/dev/null)"
[ -n "${cpu:-}" ] || cpu="$(sed -n 's/^[Hh]ardware[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | head -n 1)"
if [ -z "${cpu:-}" ] && [ -r /proc/device-tree/model ]; then
    cpu="$(tr -d '\000' </proc/device-tree/model)"
fi
[ -n "${cpu:-}" ] || cpu="RISC-V"

cores="$(awk '/^processor/ { count += 1 } END { print count + 0 }' /proc/cpuinfo 2>/dev/null)"
[ "${cores:-0}" -gt 0 ] || cores=1

mem_total_kb="$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null)"
mem_total_mb=$(( ${mem_total_kb:-0} / 1024 ))

uptime_s="$(cut -d' ' -f1 /proc/uptime)"
uptime_s="${uptime_s%%.*}"
uptime_h=$(( uptime_s / 3600 ))
uptime_m=$(( (uptime_s % 3600) / 60 ))

disk="$(df -h / 2>/dev/null | awk 'NR == 2 { print $3 " / " $2 }')"

esc="$(printf '\033')"
b="$esc[1m"; c="$esc[36m"; g="$esc[32m"; r="$esc[0m"

printf '%s\n' \
"${c}        /\\_/\\${r}      ${b}${hostname_value}${r}" \
"${c}       ( o.o )${r}     ${g}-----------------------${r}" \
"${c}        > ^ <${r}      OS      : ${pretty}" \
"                        Kernel  : Asterinas (${kernel} ABI)" \
"                        Arch    : ${machine}" \
"                        CPU     : ${cpu} (${cores}x)" \
"                        Memory  : ${mem_total_mb} MiB" \
"                        Disk /  : ${disk:-?}" \
"                        Uptime  : ${uptime_h}h ${uptime_m}m" \
"                        Desktop : Xorg + openbox (X11)"
