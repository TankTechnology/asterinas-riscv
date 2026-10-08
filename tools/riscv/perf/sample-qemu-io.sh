#!/usr/bin/env bash
# Sample the guest's host-side I/O counters while a QEMU run is in flight.
#
# QEMU with cache=directsync turns each guest block request into a host pread on
# the backing file, so /proc/<qemu-pid>/io is a proxy for how much I/O the guest
# kernel actually asked for, and how large each request was. The counters vanish
# when the process exits, so they have to be sampled during the run: the last
# sample before exit is the total.
#
# Usage: sample-qemu-io.sh <output-file> [interval-seconds]
set -uo pipefail

readonly OUT="${1:?usage: sample-qemu-io.sh <output-file> [interval]}"
readonly INTERVAL="${2:-10}"

live_qemus() {
    local p
    for p in $(pgrep -f 'qemu-syste[m]' 2>/dev/null); do
        case "$(ps -o stat= -p "$p" 2>/dev/null)" in
            Z*|"") continue ;;
        esac
        printf '%s\n' "$p"
    done
}

: >"$OUT"
# Wait for the run to start rather than sampling whatever was already running.
for _ in $(seq 1 600); do
    [[ -n "$(live_qemus)" ]] && break
    sleep 1
done

while :; do
    pid="$(live_qemus | head -1)"
    if [[ -z "$pid" ]]; then
        printf '%s EXITED\n' "$(date +%s)" >>"$OUT"
        break
    fi
    # rchar/syscr count every read syscall the process made; read_bytes counts
    # only what came off the block layer, so the pair separates "how many
    # requests" from "how much data".
    fields="$(sudo cat "/proc/$pid/io" 2>/dev/null | tr '\n' ' ')"
    printf '%s pid=%s %s\n' "$(date +%s)" "$pid" "$fields" >>"$OUT"
    sleep "$INTERVAL"
done
