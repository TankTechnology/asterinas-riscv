#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Guest side of the Debian application matrix. Reads one line per application,
# with fields separated by 0x1f (unit separator):
#
#     name <US> kind <US> package <US> command <US> expect <US> window
#
# and prints one machine-readable line per application:
#
#     APP <name> PASS|FAIL|SKIP <detail>
#
# CLI applications must exit 0 and print `expect`. GUI applications must open
# a *new* window whose name matches `window` within six seconds; a window that
# already existed (a stale window from an earlier run) does not count.

set -u

manifest=${1:-/tmp/asterinas-apps.tsv}
export DISPLAY=${DISPLAY:-:0}
separator=$(printf '\037')

report() {
    name=$1
    status=$2
    shift 2
    printf 'APP %s %s %s\n' "$name" "$status" "${1:-}"
}

run_cli() {
    name=$1
    expect=$2
    command=$3
    output=$(sh -c "$command" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$output" | grep -qF -- "$expect"; then
        report "$name" PASS "rc=0"
    else
        first=$(printf '%s' "$output" | head -1)
        report "$name" FAIL "rc=$rc out=$first"
    fi
}

run_gui() {
    name=$1
    window=$2
    command=$3
    if ! command -v xdotool >/dev/null 2>&1; then
        report "$name" SKIP "xdotool is not installed"
        return
    fi
    before=$(xdotool search --name "$window" 2>/dev/null | tr '\n' ' ')
    sh -c "exec $command" >/dev/null 2>&1 &
    pid=$!
    found=""
    for _ in 1 2 3 4 5 6; do
        sleep 1
        for id in $(xdotool search --name "$window" 2>/dev/null); do
            case " $before " in
                *" $id "*) ;;
                *) found=$id; break ;;
            esac
        done
        if [ -n "$found" ]; then
            break
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
    done
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    if [ -n "$found" ]; then
        report "$name" PASS "window=$found"
    else
        report "$name" FAIL "no new window matching $window"
    fi
}

while IFS="$separator" read -r name kind package command expect window; do
    [ -n "$name" ] || continue
    case "$kind" in
        cli) run_cli "$name" "$expect" "$command" ;;
        gui) run_gui "$name" "$window" "$command" ;;
        *) report "$name" FAIL "unknown kind $kind" ;;
    esac
done <"$manifest"

printf 'APPS_DONE\n'
