#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-120}"
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
readonly MEGREZ_FIXTURE_URL='http://10.100.19.216:17894/asterinas-network-probe.bin'
readonly QEMU_FIXTURE_URL='http://10.0.2.2:17894/asterinas-network-probe.bin'
readonly MEGREZ_FIXTURE_SIZE='65536'
readonly MEGREZ_FIXTURE_SHA256='7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2'
readonly MEGREZ_FIXTURE_REQUESTS='20'
readonly PROXY_URL="${ASTERINAS_DESKTOP_PROXY_URL:-}"
readonly PROXY_HOST="${ASTERINAS_DESKTOP_PROXY_HOST:-}"
readonly PROXY_PORT="${ASTERINAS_DESKTOP_PROXY_PORT:-}"
readonly FIXTURE_URL="${ASTERINAS_DESKTOP_FIXTURE_URL:-}"
readonly FIXTURE_SIZE="${ASTERINAS_DESKTOP_FIXTURE_SIZE:-}"
readonly FIXTURE_SHA256="${ASTERINAS_DESKTOP_FIXTURE_SHA256:-}"
readonly FIXTURE_REQUESTS="${ASTERINAS_DESKTOP_FIXTURE_REQUESTS:-}"
readonly CLOCK_URL='http://www.baidu.com/'
readonly BAIDU_URL='https://www.baidu.com/'
readonly BAIDU_ASSET='https://www.baidu.com/img/flexible/logo/pc/result.png'
readonly WEB_NETWORK_MODE="${ASTERINAS_WEB_NETWORK_MODE:-}"
readonly WEB_NETWORK_ADDRESS="${ASTERINAS_WEB_NETWORK_ADDRESS:-10.100.19.200/21}"
readonly WEB_NETWORK_GATEWAY="${ASTERINAS_WEB_NETWORK_GATEWAY:-10.100.16.1}"
readonly WEB_NETWORK_RESOLVER="${ASTERINAS_WEB_NETWORK_RESOLVER:-}"
readonly WEB_NETWORK_MEDIUM_URL="${ASTERINAS_WEB_NETWORK_MEDIUM_URL:-}"
readonly WEB_NETWORK_MEDIUM_SIZE="${ASTERINAS_WEB_NETWORK_MEDIUM_SIZE:-262144}"
readonly WEB_NETWORK_MEDIUM_SHA256="${ASTERINAS_WEB_NETWORK_MEDIUM_SHA256:-}"
LAST_LINK_OUTPUT=''
LAST_ADDRESS_OUTPUT=''
WEB_TEMPORARY_DIRECTORY=''

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_NETWORK_M5_FAIL reason=$1"
    exit 1
}

web_fail() {
    local layer="$1"
    local reason="$2"

    emit "DEBIAN_WEB_NETWORK_FAIL mode=${WEB_NETWORK_MODE:-invalid} layer=$layer reason=$reason"
    exit 1
}

web_emit_layer() {
    emit "DEBIAN_WEB_NETWORK_LAYER mode=$WEB_NETWORK_MODE layer=$1 status=pass"
}

web_cleanup() {
    if [[ -n "$WEB_TEMPORARY_DIRECTORY" && -d "$WEB_TEMPORARY_DIRECTORY" ]]; then
        find "$WEB_TEMPORARY_DIRECTORY" -mindepth 1 -maxdepth 1 -type f \
            -delete 2>/dev/null || true
        rmdir -- "$WEB_TEMPORARY_DIRECTORY" 2>/dev/null || true
    fi
}

web_timeout_seconds() {
    local deadline="$1"
    local remaining=$((deadline - SECONDS))

    ((remaining > 0)) || return 1
    if ((remaining < COMMAND_TIMEOUT_SECONDS)); then
        printf '%s\n' "$remaining"
    else
        printf '%s\n' "$COMMAND_TIMEOUT_SECONDS"
    fi
}

web_curl_reason() {
    local status="$1"

    case "$status" in
        5 | 6) printf '%s\n' dns ;;
        7)
            if [[ "$WEB_NETWORK_MODE" == proxy ]]; then
                printf '%s\n' proxy-unavailable
            else
                printf '%s\n' tcp-connect
            fi
            ;;
        22) printf '%s\n' http-status ;;
        28) printf '%s\n' timeout ;;
        35 | 51 | 58 | 59 | 60 | 77 | 80 | 82 | 83 | 90 | 91)
            printf '%s\n' tls
            ;;
        *) printf '%s\n' transport ;;
    esac
}

web_validate_ipv4() {
    local address="$1"
    local octet
    local -a octets

    [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r -a octets <<<"$address"
    for octet in "${octets[@]}"; do
        ((10#$octet <= 255)) || return 1
    done
}

web_network_evidence() {
    local address_output
    local clock_date=''
    local curl_result
    local curl_status
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    local header
    local headers
    local http_file
    local https_status
    local limit
    local link_output
    local local_address
    local neighbor_output
    local peer
    local signature
    local temporary_asset
    local temporary_medium
    local temporary_repeat
    local -a external_curl=()

    case "$WEB_NETWORK_MODE" in
        proxy)
            [[ -n "$PROXY_URL" && -n "$PROXY_HOST" && -n "$PROXY_PORT" ]] ||
                web_fail config missing-proxy
            web_validate_ipv4 "$PROXY_HOST" || web_fail config invalid-proxy
            [[ "$PROXY_PORT" =~ ^[1-9][0-9]{0,4}$ ]] ||
                web_fail config invalid-proxy
            ((PROXY_PORT <= 65535)) || web_fail config invalid-proxy
            [[ "$PROXY_URL" == "http://$PROXY_HOST:$PROXY_PORT" ]] ||
                web_fail config invalid-proxy
            peer="$PROXY_HOST"
            external_curl=(--proxy "$PROXY_URL")
            ;;
        direct)
            [[ -z "$PROXY_URL" && -z "$PROXY_HOST" && -z "$PROXY_PORT" ]] ||
                web_fail config proxy-present
            web_validate_ipv4 "$WEB_NETWORK_RESOLVER" ||
                web_fail config invalid-resolver
            peer="$WEB_NETWORK_GATEWAY"
            ;;
        *) web_fail config invalid-mode ;;
    esac
    web_validate_ipv4 "$WEB_NETWORK_GATEWAY" || web_fail config invalid-gateway
    [[ "$WEB_NETWORK_ADDRESS" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] ||
        web_fail config invalid-address
    [[ -n "$FIXTURE_URL" && "$FIXTURE_SIZE" =~ ^[1-9][0-9]*$ ]] ||
        web_fail config invalid-fixture
    [[ "$FIXTURE_SHA256" =~ ^[0-9a-f]{64}$ && "$FIXTURE_REQUESTS" == 20 ]] ||
        web_fail config invalid-fixture
    [[ -n "$WEB_NETWORK_MEDIUM_URL" && "$WEB_NETWORK_MEDIUM_SIZE" =~ ^[1-9][0-9]*$ ]] ||
        web_fail config invalid-medium
    [[ "$WEB_NETWORK_MEDIUM_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
        web_fail config invalid-medium

    WEB_TEMPORARY_DIRECTORY="$(mktemp -d "${URL_FILE}.web.XXXXXX")" ||
        web_fail config temporary-directory
    trap web_cleanup EXIT

    limit="$(web_timeout_seconds "$deadline")" || web_fail link timeout
    if ! link_output="$(timeout "$limit" ip -o link show dev "$INTERFACE" 2>/dev/null)"; then
        web_fail link carrier
    fi
    [[ "$link_output" == *"<"*UP*LOWER_UP*">"* ]] || web_fail link carrier
    web_emit_layer link

    limit="$(web_timeout_seconds "$deadline")" || web_fail address timeout
    if ! address_output="$(timeout "$limit" ip -o -4 addr show dev "$INTERFACE" scope global 2>/dev/null)"; then
        web_fail address static-address
    fi
    [[ "$address_output" =~ (^|[[:space:]])inet[[:space:]]$WEB_NETWORK_ADDRESS([[:space:]]|$) ]] ||
        web_fail address static-address
    web_emit_layer address

    limit="$(web_timeout_seconds "$deadline")" || web_fail neighbor timeout
    if ! neighbor_output="$(timeout "$limit" ip neigh show to "$peer" dev "$INTERFACE" 2>/dev/null)"; then
        web_fail neighbor neighbor-unusable
    fi
    [[ "$neighbor_output" == *"lladdr "* ]] || web_fail neighbor neighbor-unusable
    [[ "$neighbor_output" != *FAILED* && "$neighbor_output" != *INCOMPLETE* ]] ||
        web_fail neighbor neighbor-unusable
    web_emit_layer neighbor

    limit="$(web_timeout_seconds "$deadline")" || web_fail reachability timeout
    if ! timeout "$limit" ping -4 -c 1 -W 3 "$peer" >/dev/null 2>&1; then
        web_fail reachability icmp-timeout
    fi
    web_emit_layer reachability

    if [[ "$WEB_NETWORK_MODE" == direct ]]; then
        printf 'nameserver %s\n' "$WEB_NETWORK_RESOLVER" >"$RESOLV_CONF" ||
            web_fail dns resolver-write
        limit="$(web_timeout_seconds "$deadline")" || web_fail dns timeout
        if ! timeout "$limit" getent ahostsv4 www.baidu.com >/dev/null 2>&1; then
            web_fail dns resolve
        fi
    fi
    web_emit_layer dns

    http_file="$WEB_TEMPORARY_DIRECTORY/http"
    limit="$(web_timeout_seconds "$deadline")" || web_fail http timeout
    if timeout "$limit" curl --fail --ipv4 --silent --show-error \
        --max-time "$limit" --noproxy '*' --output "$http_file" "$FIXTURE_URL"; then
        :
    else
        curl_status=$?
        web_fail http "$(web_curl_reason "$curl_status")"
    fi
    [[ "$(stat -c '%s' -- "$http_file")" == "$FIXTURE_SIZE" ]] ||
        web_fail http content
    [[ "$(sha256sum -- "$http_file" | awk '{print $1}')" == "$FIXTURE_SHA256" ]] ||
        web_fail http content

    limit="$(web_timeout_seconds "$deadline")" || web_fail http timeout
    if headers="$(timeout "$limit" curl --fail --head --ipv4 --silent \
        --show-error --max-time "$limit" "${external_curl[@]}" "$CLOCK_URL")"; then
        :
    else
        curl_status=$?
        web_fail http "$(web_curl_reason "$curl_status")"
    fi
    while IFS= read -r header; do
        header="${header%$'\r'}"
        if [[ "${header,,}" == date:* ]]; then
            clock_date="${header#*:}"
            clock_date="${clock_date#${clock_date%%[![:space:]]*}}"
            break
        fi
    done <<<"$headers"
    [[ "$clock_date" =~ ^[A-Z][a-z]{2},\ [0-9]{2}\ [A-Z][a-z]{2}\ [0-9]{4}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ GMT$ ]] ||
        web_fail http date-header
    date --utc --set "$clock_date" >/dev/null || web_fail http clock-set
    web_emit_layer http

    limit="$(web_timeout_seconds "$deadline")" || web_fail https timeout
    if curl_result="$(timeout "$limit" curl --fail --ipv4 --location --silent \
        --show-error --max-time "$limit" "${external_curl[@]}" \
        --output /dev/null --write-out $'%{http_code}\t%{local_ip}\t%{time_connect}\t%{time_appconnect}' \
        "$BAIDU_URL")"; then
        :
    else
        curl_status=$?
        web_fail https "$(web_curl_reason "$curl_status")"
    fi
    IFS=$'\t' read -r https_status local_address _ _ <<<"$curl_result"
    [[ "$https_status" =~ ^(2|3)[0-9][0-9]$ ]] || web_fail https http-status
    [[ "$local_address" == "${WEB_NETWORK_ADDRESS%/*}" ]] ||
        web_fail https local-address
    web_emit_layer https

    temporary_asset="$WEB_TEMPORARY_DIRECTORY/baidu-logo.png"
    limit="$(web_timeout_seconds "$deadline")" || web_fail baidu-asset timeout
    if timeout "$limit" curl --fail --ipv4 --location --silent --show-error \
        --max-time "$limit" "${external_curl[@]}" --output "$temporary_asset" \
        "$BAIDU_ASSET"; then
        :
    else
        curl_status=$?
        web_fail baidu-asset "$(web_curl_reason "$curl_status")"
    fi
    signature="$(od -An -N8 -tx1 "$temporary_asset" | tr -d '[:space:]')"
    [[ "$signature" == 89504e470d0a1a0a ]] || web_fail baidu-asset content
    web_emit_layer baidu-asset

    # The HTTP layer already verified the first deterministic fixture response.
    # Download 19 more so the complete mode contract records exactly 20.
    for ((attempt = 1; attempt < FIXTURE_REQUESTS; attempt++)); do
        temporary_repeat="$WEB_TEMPORARY_DIRECTORY/repeat-$attempt"
        limit="$(web_timeout_seconds "$deadline")" || web_fail repeat timeout
        if timeout "$limit" curl --fail --ipv4 --silent --show-error \
            --max-time "$limit" --noproxy '*' --output "$temporary_repeat" \
            "$FIXTURE_URL"; then
            :
        else
            curl_status=$?
            web_fail repeat "$(web_curl_reason "$curl_status")"
        fi
        [[ "$(stat -c '%s' -- "$temporary_repeat")" == "$FIXTURE_SIZE" ]] ||
            web_fail repeat length
        [[ "$(sha256sum -- "$temporary_repeat" | awk '{print $1}')" == "$FIXTURE_SHA256" ]] ||
            web_fail repeat digest
    done
    web_emit_layer repeat

    temporary_medium="$WEB_TEMPORARY_DIRECTORY/medium"
    limit="$(web_timeout_seconds "$deadline")" || web_fail medium timeout
    if timeout "$limit" curl --fail --ipv4 --silent --show-error \
        --max-time "$limit" --noproxy '*' --output "$temporary_medium" \
        "$WEB_NETWORK_MEDIUM_URL"; then
        :
    else
        curl_status=$?
        web_fail medium "$(web_curl_reason "$curl_status")"
    fi
    [[ "$(stat -c '%s' -- "$temporary_medium")" == "$WEB_NETWORK_MEDIUM_SIZE" ]] ||
        web_fail medium length
    [[ "$(sha256sum -- "$temporary_medium" | awk '{print $1}')" == "$WEB_NETWORK_MEDIUM_SHA256" ]] ||
        web_fail medium digest
    web_emit_layer medium

    publish_remote_url
    emit "DEBIAN_WEB_NETWORK_READY mode=$WEB_NETWORK_MODE layers=10"
}

link_and_address_ready() {
    local flags

    if LAST_LINK_OUTPUT="$(ip -o link show dev "$INTERFACE" 2>/dev/null)"; then
        :
    else
        return 1
    fi
    [[ "$LAST_LINK_OUTPUT" == *"<"*">"* ]] || return 1
    flags="${LAST_LINK_OUTPUT#*<}"
    flags="${flags%%>*}"
    case ",$flags," in
        *,UP,*) ;;
        *) return 1 ;;
    esac
    case ",$flags," in
        *,LOWER_UP,*) ;;
        *) return 1 ;;
    esac
    if LAST_ADDRESS_OUTPUT="$(ip -o -4 addr show dev "$INTERFACE" scope global 2>/dev/null)"; then
        :
    else
        return 1
    fi
    [[ "$LAST_ADDRESS_OUTPUT" =~ (^|[[:space:]])inet[[:space:]]$ADDRESS([[:space:]]|$) ]]
}

emit_link_diagnostic() {
    local field="$1"
    local output
    local value_hex

    case "$field" in
        link) output="$LAST_LINK_OUTPUT" ;;
        address) output="$LAST_ADDRESS_OUTPUT" ;;
        *) output='' ;;
    esac
    value_hex="$(printf '%s' "$output" | od -An -v -tx1 | tr -d '[:space:]')"
    emit "DEBIAN_NETWORK_M5_DIAGNOSTIC phase=link-check field=$field status=0 value_hex=${value_hex:-none}"
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
                --write-out $'%{http_code}\t%{local_ip}\t%{time_namelookup}\t%{time_connect}\t%{time_appconnect}\t%{time_starttransfer}' \
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

synchronize_megrez_clock() {
    local clock_date=''
    local header
    local headers

    if ! headers="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" curl \
            --fail \
            --head \
            --ipv4 \
            --silent \
            --show-error \
            --max-time "$COMMAND_TIMEOUT_SECONDS" \
            --proxy "$PROXY_URL" \
            "$CLOCK_URL"
    )"; then
        fail megrez-clock
    fi
    while IFS= read -r header; do
        header="${header%$'\r'}"
        if [[ "${header,,}" == date:* ]]; then
            clock_date="${header#*:}"
            clock_date="${clock_date#${clock_date%%[![:space:]]*}}"
            break
        fi
    done <<<"$headers"
    [[ "$clock_date" =~ ^[A-Z][a-z]{2},\ [0-9]{2}\ [A-Z][a-z]{2}\ [0-9]{4}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ GMT$ ]] ||
        fail megrez-clock-date
    date --utc --set "$clock_date" >/dev/null || fail megrez-clock-set
    emit "DEBIAN_NETWORK_M5_CLOCK source=http-date proxy=$PROXY_HOST:$PROXY_PORT"
}

validate_fixture_config() {
    local expected_url="$1"

    [[ "$FIXTURE_URL" == "$expected_url" ]] || return 1
    [[ "$FIXTURE_SIZE" == "$MEGREZ_FIXTURE_SIZE" ]] || return 1
    [[ "$FIXTURE_SHA256" == "$MEGREZ_FIXTURE_SHA256" ]] || return 1
    [[ "$FIXTURE_REQUESTS" == "$MEGREZ_FIXTURE_REQUESTS" ]] ||
        return 1
}

cleanup_fixture_batch() {
    local directory="$1"
    shift

    rm -f -- "$@" && rmdir -- "$directory"
}

stress_fixture() {
    local attempt
    local deadline="$1"
    local endpoint="$2"
    local hashes
    local reason_prefix="$3"
    local remaining
    local sizes
    local temporary_directory
    local -a curl_arguments=(
        --fail
        --ipv4
        --silent
        --show-error
        --max-time "$COMMAND_TIMEOUT_SECONDS"
        --noproxy '*'
    )
    local -a temporary_fixtures=()

    temporary_directory="$(mktemp -d "${URL_FILE}.fixture.XXXXXX")" ||
        fail "${reason_prefix}-fixture-temporary"
    for ((attempt = 1; attempt <= FIXTURE_REQUESTS; attempt++)); do
        temporary_fixtures+=("$temporary_directory/$attempt")
        curl_arguments+=(--output "${temporary_fixtures[-1]}" "$FIXTURE_URL")
    done

    remaining=$((deadline - SECONDS))
    if ((remaining <= 0)); then
        cleanup_fixture_batch "$temporary_directory" "${temporary_fixtures[@]}" ||
            fail "${reason_prefix}-fixture-cleanup"
        fail "${reason_prefix}-fixture-timeout"
    fi
    if ! timeout "$remaining" curl "${curl_arguments[@]}"; then
        cleanup_fixture_batch "$temporary_directory" "${temporary_fixtures[@]}" ||
            fail "${reason_prefix}-fixture-cleanup"
        fail "${reason_prefix}-fixture-download"
    fi

    sizes="$(stat -c '%s' -- "${temporary_fixtures[@]}")" || {
        cleanup_fixture_batch "$temporary_directory" "${temporary_fixtures[@]}" ||
            fail "${reason_prefix}-fixture-cleanup"
        fail "${reason_prefix}-fixture-size"
    }
    while IFS= read -r size; do
        if [[ "$size" != "$FIXTURE_SIZE" ]]; then
            cleanup_fixture_batch \
                "$temporary_directory" "${temporary_fixtures[@]}" ||
                fail "${reason_prefix}-fixture-cleanup"
            fail "${reason_prefix}-fixture-size"
        fi
    done <<<"$sizes"

    hashes="$(sha256sum -- "${temporary_fixtures[@]}")" || {
        cleanup_fixture_batch "$temporary_directory" "${temporary_fixtures[@]}" ||
            fail "${reason_prefix}-fixture-cleanup"
        fail "${reason_prefix}-fixture-sha256"
    }
    while IFS=' ' read -r hash _; do
        if [[ "$hash" != "$FIXTURE_SHA256" ]]; then
            cleanup_fixture_batch \
                "$temporary_directory" "${temporary_fixtures[@]}" ||
                fail "${reason_prefix}-fixture-cleanup"
            fail "${reason_prefix}-fixture-sha256"
        fi
    done <<<"$hashes"

    cleanup_fixture_batch "$temporary_directory" "${temporary_fixtures[@]}" ||
        fail "${reason_prefix}-fixture-cleanup"
    emit "DEBIAN_NETWORK_M5_STRESS requests=$FIXTURE_REQUESTS bytes=$((FIXTURE_SIZE * FIXTURE_REQUESTS)) sha256=$FIXTURE_SHA256 endpoint=$endpoint"
}

qemu_network_evidence() {
    local curl_result
    local deadline
    local http_status
    local local_address
    local lookup_time
    local connect_time
    local tls_time
    local first_byte_time

    validate_fixture_config "$QEMU_FIXTURE_URL" || fail qemu-fixture-config
    deadline=$((SECONDS + TIMEOUT_SECONDS))
    stress_fixture "$deadline" '10.0.2.2:17894' qemu
    printf '%s\n' 'nameserver 10.0.2.3' >"$RESOLV_CONF" || fail resolver-write
    if [[ "${ASTERINAS_DESKTOP_M5_NETWORK_MODE:-full}" == lightweight ]]; then
        # Browser startup must not wait for a remote TLS probe.  The browser
        # evidence service performs the authoritative DNS/TLS checks after the
        # desktop and Firefox can start.  Do not make this dependency rely on
        # `ip` route formatting: that interface is still incomplete in some
        # Asterinas profiles, while getent/curl below are the authoritative
        # connectivity checks.
        emit "DEBIAN_NETWORK_M5_QEMU_READY mode=qemu-slirp-lightweight"
        return 0
    fi
    resolve_qemu_host www.baidu.com \
        >/dev/null 2>>"$CONSOLE" || fail qemu-dns
    emit "DEBIAN_NETWORK_M5_QEMU_DNS resolver=10.0.2.3 host=www.baidu.com"
    curl_result="$(request_qemu_https)" || fail qemu-https
    [[ "$curl_result" == *$'\t'* ]] || fail qemu-curl-output
    http_status="${curl_result%%$'\t'*}"
    local_address="${curl_result#*$'\t'}"
    if [[ "$local_address" == *$'\t'* ]]; then
        lookup_time="${local_address#*$'\t'}"
        local_address="${local_address%%$'\t'*}"
        connect_time="${lookup_time#*$'\t'}"
        lookup_time="${lookup_time%%$'\t'*}"
        tls_time="${connect_time#*$'\t'}"
        connect_time="${connect_time%%$'\t'*}"
        first_byte_time="${tls_time#*$'\t'}"
        tls_time="${tls_time%%$'\t'*}"
        emit "DEBIAN_NETWORK_M5_QEMU_TIMING host=www.baidu.com namelookup=${lookup_time:-unknown} connect=${connect_time:-unknown} appconnect=${tls_time:-unknown} starttransfer=${first_byte_time:-unknown}"
    fi
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
    validate_fixture_config "$MEGREZ_FIXTURE_URL" || fail megrez-fixture-config

    deadline=$((SECONDS + TIMEOUT_SECONDS))
    while ! link_and_address_ready; do
        if ((SECONDS >= deadline)); then
            emit_link_diagnostic link
            emit_link_diagnostic address
            fail link-or-address-timeout
        fi
        sleep 1
    done

    emit "DEBIAN_NETWORK_M5_LINK interface=$INTERFACE address=$ADDRESS state=lower-up"
    emit "DEBIAN_NETWORK_M5_MEGREZ_PROXY endpoint=$PROXY_HOST:$PROXY_PORT"
    emit "DEBIAN_NETWORK_M5_STRESS_START requests=$FIXTURE_REQUESTS endpoint=$PROXY_HOST:17894"
    stress_fixture "$deadline" '10.100.19.216:17894' megrez
    synchronize_megrez_clock

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
if [[ -n "$WEB_NETWORK_MODE" ]]; then
    web_network_evidence
    exit 0
fi
if grep -Eq '(^|[[:space:]])asterinas\.debian_network=qemu-slirp([[:space:]]|$)' \
    "$CMDLINE_PATH"; then
    qemu_network_evidence
    exit 0
fi
megrez_network_evidence
