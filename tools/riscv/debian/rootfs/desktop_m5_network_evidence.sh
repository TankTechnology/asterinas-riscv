#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-60}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_COMMAND_TIMEOUT_SECONDS:-30}"
readonly CMDLINE_PATH="${ASTERINAS_DESKTOP_M5_CMDLINE_PATH:-/proc/cmdline}"
readonly RESOLV_CONF="${ASTERINAS_DESKTOP_M5_RESOLV_CONF:-/etc/resolv.conf}"
readonly URL_FILE="${ASTERINAS_DESKTOP_M5_URL_FILE:-/run/asterinas-desktop-url}"
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

qemu_network_evidence() {
    local curl_result
    local http_status
    local local_address
    local temporary_url

    printf '%s\n' 'nameserver 10.0.2.3' >"$RESOLV_CONF" || fail resolver-write
    timeout "$COMMAND_TIMEOUT_SECONDS" getent ahostsv4 www.baidu.com \
        >/dev/null 2>>"$CONSOLE" || fail qemu-dns
    emit "DEBIAN_NETWORK_M5_QEMU_DNS resolver=10.0.2.3 host=www.baidu.com"
    curl_result="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" curl \
            --fail \
            --location \
            --silent \
            --show-error \
            --max-time "$COMMAND_TIMEOUT_SECONDS" \
            --output /dev/null \
            --write-out $'%{http_code}\t%{local_ip}' \
            https://www.baidu.com/
    )" || fail qemu-https
    [[ "$curl_result" == *$'\t'* ]] || fail qemu-curl-output
    http_status="${curl_result%%$'\t'*}"
    local_address="${curl_result#*$'\t'}"
    [[ "$http_status" =~ ^(2|3)[0-9][0-9]$ ]] || fail qemu-http-status
    [[ "$local_address" == "10.0.2.15" ]] || fail qemu-local-address
    emit "DEBIAN_NETWORK_M5_QEMU_HTTPS host=www.baidu.com status=$http_status address=$local_address"

    temporary_url="$(mktemp "${URL_FILE}.tmp.XXXXXX")" || fail url-temporary
    trap 'rm -f -- "$temporary_url"' RETURN
    chmod 0644 -- "$temporary_url" || fail url-mode
    printf '%s\n' 'https://www.baidu.com/' >"$temporary_url" || fail url-write
    mv -T -- "$temporary_url" "$URL_FILE" || fail url-publish
    temporary_url=""
    trap - RETURN
    emit "DEBIAN_NETWORK_M5_QEMU_READY mode=qemu-slirp"
}

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-command-timeout
if grep -Eq '(^|[[:space:]])asterinas\.debian_network=qemu-slirp([[:space:]]|$)' \
    "$CMDLINE_PATH"; then
    qemu_network_evidence
    exit 0
fi
deadline=$((SECONDS + TIMEOUT_SECONDS))
while ! link_and_address_ready; do
    ((SECONDS < deadline)) || fail link-or-address-timeout
    sleep 1
done

emit "DEBIAN_NETWORK_M5_LINK interface=$INTERFACE address=$ADDRESS state=lower-up"
ping -n -c 10 -W 2 "$PEER" >>"$CONSOLE" 2>&1 || fail guest-ping
emit "DEBIAN_NETWORK_M5_GUEST_PING peer=$PEER count=10"
emit "DEBIAN_NETWORK_M5_READY interface=$INTERFACE"
