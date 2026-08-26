#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-60}"
readonly INTERFACE="eth0"
readonly ADDRESS="10.100.19.200/21"
readonly PEER="10.100.19.216"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_NETWORK_M5_FAIL reason=$1"
    exit 1
}

link_and_address_ready() {
    local addresses
    local flags
    local link

    link="$(ip -o link show dev "$INTERFACE" 2>/dev/null)" || return 1
    [[ "$link" == *"<"*">"* ]] || return 1
    flags="${link#*<}"
    flags="${flags%%>*}"
    case ",$flags," in
        *,UP,*) ;;
        *) return 1 ;;
    esac
    case ",$flags," in
        *,LOWER_UP,*) ;;
        *) return 1 ;;
    esac
    addresses="$(ip -o -4 addr show dev "$INTERFACE" scope global 2>/dev/null)" ||
        return 1
    [[ "$addresses" =~ (^|[[:space:]])inet[[:space:]]$ADDRESS([[:space:]]|$) ]]
}

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))
while ! link_and_address_ready; do
    ((SECONDS < deadline)) || fail link-or-address-timeout
    sleep 1
done

emit "DEBIAN_NETWORK_M5_LINK interface=$INTERFACE address=$ADDRESS state=lower-up"
ping -n -c 10 -W 2 "$PEER" >>"$CONSOLE" 2>&1 || fail guest-ping
emit "DEBIAN_NETWORK_M5_GUEST_PING peer=$PEER count=10"
emit "DEBIAN_NETWORK_M5_READY interface=$INTERFACE"
