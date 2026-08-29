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
readonly MEGREZ_BOOTARG='asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1'
readonly MEGREZ_PROXY_URL='http://10.100.19.216:17893'
readonly MEGREZ_PROXY_HOST='10.100.19.216'
readonly MEGREZ_PROXY_PORT='17893'
readonly PROXY_URL="${ASTERINAS_DESKTOP_PROXY_URL:-}"
readonly PROXY_HOST="${ASTERINAS_DESKTOP_PROXY_HOST:-}"
readonly PROXY_PORT="${ASTERINAS_DESKTOP_PROXY_PORT:-}"
readonly BAIDU_URL='https://www.baidu.com/'
readonly BAIDU_ASSET='https://www.baidu.com/img/flexible/logo/pc/result.png'

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

publish_remote_url() {
    local temporary_url

    temporary_url="$(mktemp "${URL_FILE}.tmp.XXXXXX")" || fail url-temporary
    if ! chmod 0644 -- "$temporary_url"; then
        rm -f -- "$temporary_url"
        fail url-mode
    fi
    if ! printf '%s\n' "$BAIDU_ASSET" >"$temporary_url"; then
        rm -f -- "$temporary_url"
        fail url-write
    fi
    if ! mv -T -- "$temporary_url" "$URL_FILE"; then
        rm -f -- "$temporary_url"
        fail url-publish
    fi
}

resolve_qemu_host() {
    local attempt
    local host="$1"

    for attempt in 1 2 3; do
        if timeout "$COMMAND_TIMEOUT_SECONDS" getent ahostsv4 "$host"; then
            return 0
        fi
        if ((attempt == 3)); then
            return 1
        fi
        sleep 1
    done
}

request_qemu_https() {
    local attempt
    local curl_error
    local curl_result
    local stderr_hex

    curl_error="$(mktemp "${URL_FILE}.curl-error.XXXXXX")" ||
        fail qemu-curl-temporary
    for attempt in 1 2 3; do
        : >"$curl_error" || fail qemu-curl-error-reset
        if curl_result="$(
            timeout "$COMMAND_TIMEOUT_SECONDS" curl \
                --fail \
                --location \
                --silent \
                --show-error \
                --max-time "$COMMAND_TIMEOUT_SECONDS" \
                --output /dev/null \
                --write-out $'%{http_code}\t%{local_ip}' \
                "$BAIDU_URL" 2>"$curl_error"
        )"; then
            rm -f -- "$curl_error"
            printf '%s' "$curl_result"
            return 0
        fi
        if ((attempt != 3)); then
            sleep 1
        fi
    done

    stderr_hex="$(
        head -c 2048 -- "$curl_error" |
            od -An -v -tx1 |
            tr -d '[:space:]'
    )"
    emit "DEBIAN_NETWORK_M5_DIAGNOSTIC phase=qemu-https attempt=3 stderr_hex=${stderr_hex:-none}"
    rm -f -- "$curl_error"
    return 1
}

request_megrez_https() {
    local attempt
    local curl_error
    local curl_result
    local curl_status=1
    local stderr_hex

    curl_error="$(mktemp "${URL_FILE}.curl-error.XXXXXX")" ||
        fail megrez-curl-temporary
    for attempt in 1 2 3; do
        : >"$curl_error" || fail megrez-curl-error-reset
        if curl_result="$(
            timeout "$COMMAND_TIMEOUT_SECONDS" curl \
                --fail \
                --ipv4 \
                --location \
                --silent \
                --show-error \
                --max-time "$COMMAND_TIMEOUT_SECONDS" \
                --proxy "$PROXY_URL" \
                --output /dev/null \
                --write-out $'%{http_code}\t%{local_ip}' \
                "$BAIDU_URL" 2>"$curl_error"
        )"; then
            rm -f -- "$curl_error"
            printf '%s' "$curl_result"
            return 0
        else
            curl_status=$?
        fi
        if ((attempt != 3)); then
            sleep 1
        fi
    done

    stderr_hex="$(
        head -c 2048 -- "$curl_error" |
            od -An -v -tx1 |
            tr -d '[:space:]'
    )"
    emit "DEBIAN_NETWORK_M5_DIAGNOSTIC phase=megrez-https attempt=3 status=$curl_status stderr_hex=${stderr_hex:-none}"
    rm -f -- "$curl_error"
    return 1
}

qemu_network_evidence() {
    local curl_result
    local http_status
    local local_address

    printf '%s\n' 'nameserver 10.0.2.3' >"$RESOLV_CONF" || fail resolver-write
    resolve_qemu_host www.baidu.com \
        >/dev/null 2>>"$CONSOLE" || fail qemu-dns
    emit "DEBIAN_NETWORK_M5_QEMU_DNS resolver=10.0.2.3 host=www.baidu.com"
    curl_result="$(request_qemu_https)" || fail qemu-https
    [[ "$curl_result" == *$'\t'* ]] || fail qemu-curl-output
    http_status="${curl_result%%$'\t'*}"
    local_address="${curl_result#*$'\t'}"
    [[ "$http_status" =~ ^(2|3)[0-9][0-9]$ ]] || fail qemu-http-status
    [[ "$local_address" == "10.0.2.15" ]] || fail qemu-local-address
    emit "DEBIAN_NETWORK_M5_QEMU_HTTPS host=www.baidu.com status=$http_status address=$local_address"

    publish_remote_url
    emit "DEBIAN_NETWORK_M5_QEMU_READY mode=qemu-slirp"
}

megrez_network_evidence() {
    local curl_result
    local deadline
    local http_status
    local local_address
    local temporary_asset

    tr '[:space:]' '\n' <"$CMDLINE_PATH" | grep -Fxq "$MEGREZ_BOOTARG" ||
        fail megrez-bootarg
    [[ "$PROXY_URL" == "$MEGREZ_PROXY_URL" ]] || fail megrez-proxy-config
    [[ "$PROXY_HOST" == "$MEGREZ_PROXY_HOST" ]] || fail megrez-proxy-config
    [[ "$PROXY_PORT" == "$MEGREZ_PROXY_PORT" ]] || fail megrez-proxy-config

    deadline=$((SECONDS + TIMEOUT_SECONDS))
    while ! link_and_address_ready; do
        ((SECONDS < deadline)) || fail link-or-address-timeout
        sleep 1
    done

    emit "DEBIAN_NETWORK_M5_LINK interface=$INTERFACE address=$ADDRESS state=lower-up"
    emit "DEBIAN_NETWORK_M5_MEGREZ_PROXY endpoint=$PROXY_HOST:$PROXY_PORT"

    curl_result="$(request_megrez_https)" || fail megrez-https
    [[ "$curl_result" == *$'\t'* ]] || fail megrez-curl-output
    http_status="${curl_result%%$'\t'*}"
    local_address="${curl_result#*$'\t'}"
    [[ "$http_status" == 200 ]] || fail megrez-http-status
    [[ "$local_address" == "10.100.19.200" ]] || fail megrez-local-address
    emit "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=$http_status address=$local_address proxy=$PROXY_HOST:$PROXY_PORT"

    temporary_asset="$(mktemp "${URL_FILE}.asset.XXXXXX")" || fail asset-temporary
    if ! timeout "$COMMAND_TIMEOUT_SECONDS" curl \
        --fail \
        --ipv4 \
        --location \
        --silent \
        --show-error \
        --max-time "$COMMAND_TIMEOUT_SECONDS" \
        --proxy "$PROXY_URL" \
        --output "$temporary_asset" \
        "$BAIDU_ASSET"; then
        rm -f -- "$temporary_asset"
        fail megrez-asset
    fi
    if [[ ! -s "$temporary_asset" ]]; then
        rm -f -- "$temporary_asset"
        fail megrez-asset
    fi
    rm -f -- "$temporary_asset" || fail asset-cleanup
    emit "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png proxy=$PROXY_HOST:$PROXY_PORT"

    publish_remote_url
    emit "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45-host-proxy"
}

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-command-timeout
if grep -Eq '(^|[[:space:]])asterinas\.debian_network=qemu-slirp([[:space:]]|$)' \
    "$CMDLINE_PATH"; then
    qemu_network_evidence
    exit 0
fi
megrez_network_evidence
