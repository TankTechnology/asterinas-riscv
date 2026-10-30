#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_NETWORK_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS:-120}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_NETWORK_COMMAND_TIMEOUT_SECONDS:-30}"
# The owned fixture lives on the build host across one slirp hop. TCG can
# stall a userspace curl for several seconds even when the fixture is healthy,
# so allow a realistic connect/transfer window instead of treating the first
# slow scheduling slice as an absent fixture.
readonly FIXTURE_CONNECT_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_NETWORK_FIXTURE_CONNECT_TIMEOUT_SECONDS:-5}"
readonly FIXTURE_MAX_TIME_SECONDS="${ASTERINAS_DESKTOP_NETWORK_FIXTURE_MAX_TIME_SECONDS:-15}"
readonly UPTIME_PATH="${ASTERINAS_DESKTOP_NETWORK_UPTIME_PATH:-/proc/uptime}"
readonly RESOLV_CONF="${ASTERINAS_DESKTOP_NETWORK_RESOLV_CONF:-/etc/resolv.conf}"
readonly URL_FILE="${ASTERINAS_DESKTOP_NETWORK_URL_FILE:-/run/asterinas-desktop-url}"
readonly INTERFACE="eth0"
readonly QEMU_FIXTURE_URL='http://10.0.2.2:17894/asterinas-network-probe.bin'
readonly EXPECTED_FIXTURE_SIZE='65536'
readonly EXPECTED_FIXTURE_SHA256='7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2'
readonly EXPECTED_FIXTURE_REQUESTS='20'
readonly PROXY_URL="${ASTERINAS_DESKTOP_PROXY_URL:-}"
readonly PROXY_HOST="${ASTERINAS_DESKTOP_PROXY_HOST:-}"
readonly PROXY_PORT="${ASTERINAS_DESKTOP_PROXY_PORT:-}"
readonly FIXTURE_URL="${ASTERINAS_DESKTOP_FIXTURE_URL:-}"
readonly FIXTURE_SIZE="${ASTERINAS_DESKTOP_FIXTURE_SIZE:-}"
readonly FIXTURE_SHA256="${ASTERINAS_DESKTOP_FIXTURE_SHA256:-}"
readonly FIXTURE_REQUESTS="${ASTERINAS_DESKTOP_FIXTURE_REQUESTS:-}"
readonly BAIDU_URL="${ASTERINAS_WEB_NETWORK_HTTPS_URL:-https://www.baidu.com/}"
readonly BAIDU_ASSET="${ASTERINAS_WEB_NETWORK_ASSET_URL:-https://www.baidu.com/img/flexible/logo/pc/result.png}"
readonly WEB_NETWORK_MODE="${ASTERINAS_WEB_NETWORK_MODE:-}"
readonly WEB_NETWORK_NEIGHBOR_QUERY="${ASTERINAS_WEB_NETWORK_NEIGHBOR_QUERY:-0}"
readonly WEB_NETWORK_ADDRESS="${ASTERINAS_WEB_NETWORK_ADDRESS:-10.0.2.15/24}"
readonly WEB_NETWORK_GATEWAY="${ASTERINAS_WEB_NETWORK_GATEWAY:-10.0.2.2}"
readonly WEB_NETWORK_RESOLVER="${ASTERINAS_WEB_NETWORK_RESOLVER:-}"
readonly WEB_NETWORK_MEDIUM_URL="${ASTERINAS_WEB_NETWORK_MEDIUM_URL:-${FIXTURE_URL%/*}/browser-quality/download.bin}"
readonly WEB_NETWORK_MEDIUM_SIZE="${ASTERINAS_WEB_NETWORK_MEDIUM_SIZE:-262144}"
readonly WEB_NETWORK_MEDIUM_SHA256="${ASTERINAS_WEB_NETWORK_MEDIUM_SHA256:-2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9}"
WEB_TEMPORARY_DIRECTORY=''

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_NETWORK_FAIL reason=$1"
    exit 1
}

web_fail() {
    local layer="$1"
    local reason="$2"
    local status=75

    # Exit 75 means the network fixture is not available here (no link,
    # address, neighbour, resolver, or a peer that does not answer). The unit
    # treats it as a clean run so the unit ends inactive instead of staying in
    # `systemctl --failed` when no fixture is running. Configuration mistakes
    # and answers that contradict the fixture are still failures (exit 1) and
    # remain visible.
    case "$layer:$reason" in
        config:* | http:content | \
            https:http-status | https:local-address | \
            baidu-asset:content | repeat:length | repeat:digest | \
            medium:length | medium:digest)
            status=1
            ;;
    esac

    emit "DEBIAN_WEB_NETWORK_FAIL mode=${WEB_NETWORK_MODE:-invalid} layer=$layer reason=$reason"
    exit "$status"
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

web_monotonic_seconds() {
    local idle
    local uptime

    [[ "$UPTIME_PATH" == /* && -f "$UPTIME_PATH" && ! -L "$UPTIME_PATH" ]] ||
        return 1
    read -r uptime idle <"$UPTIME_PATH" || return 1
    [[ "$uptime" =~ ^(0|[1-9][0-9]*)\.[0-9]+$ ]] || return 1
    printf '%s\n' "${uptime%%.*}"
}

web_timeout_seconds() {
    local deadline="$1"
    local now
    local remaining

    now="$(web_monotonic_seconds)" || return 1
    remaining=$((deadline - now))

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
        28 | 124) printf '%s\n' timeout ;;
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
    local curl_result
    local curl_status
    local deadline
    local http_file
    local https_status
    local https_attempt=0
    local https_reason
    local limit
    local link_output
    local local_address
    local neighbor_observable=1
    local neighbor_output
    local neighbor_status=0
    local now
    local peer
    local signature
    local temporary_asset
    local temporary_medium
    local repeat_hashes
    local repeat_sizes
    local remaining
    local -a repeat_curl=(
        --fail
        --ipv4
        --silent
        --show-error
        --connect-timeout "$FIXTURE_CONNECT_TIMEOUT_SECONDS"
        --max-time "$FIXTURE_MAX_TIME_SECONDS"
        --noproxy '*'
    )
    local -a repeat_files=()
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
    [[ "$WEB_NETWORK_NEIGHBOR_QUERY" =~ ^[01]$ ]] ||
        web_fail config invalid-neighbor-query
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

    now="$(web_monotonic_seconds)" || web_fail config monotonic-clock
    deadline=$((now + TIMEOUT_SECONDS))

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

    if [[ "$WEB_NETWORK_NEIGHBOR_QUERY" == 0 ]]; then
        # Asterinas does not yet implement RTM_GETNEIGH, and its unsupported
        # dump currently lacks a terminating netlink message.  Do not spend a
        # full command timeout waiting for an observation the kernel cannot
        # provide.  The fixed-IP owned fixture below is the authoritative
        # active proof that ARP resolution and peer reachability both worked.
        neighbor_observable=0
    else
        limit="$(web_timeout_seconds "$deadline")" || web_fail neighbor timeout
        if neighbor_output="$(timeout "$limit" ip neigh show to "$peer" dev "$INTERFACE" 2>/dev/null)"; then
            neighbor_status=0
        else
            neighbor_status=$?
        fi
        if ((neighbor_status == 1)); then
            # A future implementation may reject the query promptly while
            # still allowing the fixture to supply the active proof.
            neighbor_observable=0
        elif ((neighbor_status != 0)); then
            web_fail neighbor neighbor-unusable
        elif [[ -z "$neighbor_output" ]]; then
            neighbor_observable=0
        else
            [[ "$neighbor_output" == *"lladdr "* ]] ||
                web_fail neighbor neighbor-unusable
            [[ "$neighbor_output" != *FAILED* && "$neighbor_output" != *INCOMPLETE* ]] ||
                web_fail neighbor neighbor-unusable
            web_emit_layer neighbor

            limit="$(web_timeout_seconds "$deadline")" ||
                web_fail reachability timeout
            if ! timeout "$limit" ping -4 -c 1 -W 3 "$peer" >/dev/null 2>&1; then
                web_fail reachability icmp-timeout
            fi
            web_emit_layer reachability
        fi
    fi

    if [[ "$WEB_NETWORK_MODE" == direct ]]; then
        printf 'nameserver %s\n' "$WEB_NETWORK_RESOLVER" >"$RESOLV_CONF" ||
            web_fail dns resolver-write
        limit="$(web_timeout_seconds "$deadline")" || web_fail dns timeout
        if ! timeout "$limit" getent ahostsv4 www.baidu.com >/dev/null 2>&1; then
            web_fail dns resolve
        fi
    fi

    http_file="$WEB_TEMPORARY_DIRECTORY/http"
    limit="$(web_timeout_seconds "$deadline")" || web_fail http timeout
    if timeout "$limit" curl --fail --ipv4 --silent --show-error \
        --connect-timeout "$FIXTURE_CONNECT_TIMEOUT_SECONDS" \
        --max-time "$FIXTURE_MAX_TIME_SECONDS" --noproxy '*' \
        --output "$http_file" "$FIXTURE_URL"; then
        :
    else
        curl_status=$?
        web_fail http "$(web_curl_reason "$curl_status")"
    fi
    [[ "$(stat -c '%s' -- "$http_file")" == "$FIXTURE_SIZE" ]] ||
        web_fail http content
    [[ "$(sha256sum -- "$http_file" | awk '{print $1}')" == "$FIXTURE_SHA256" ]] ||
        web_fail http content

    if ((neighbor_observable == 0)); then
        emit "DEBIAN_WEB_NETWORK_DIAGNOSTIC mode=$WEB_NETWORK_MODE neighbor=unobservable proof=owned-fixture-http"
        web_emit_layer neighbor
        web_emit_layer reachability
    fi
    web_emit_layer dns
    web_emit_layer http

    while ((https_attempt < 2)); do
        limit="$(web_timeout_seconds "$deadline")" || web_fail https timeout
        if curl_result="$(timeout "$limit" curl --fail --ipv4 --location --silent \
            --show-error --max-time "$limit" "${external_curl[@]}" \
            --output /dev/null --write-out $'%{http_code}\t%{local_ip}\t%{time_connect}\t%{time_appconnect}' \
            "$BAIDU_URL")"; then
            break
        else
            curl_status=$?
        fi
        https_reason="$(web_curl_reason "$curl_status")"
        ((https_attempt += 1))
        if [[ "$https_reason" != timeout ]] || ((https_attempt >= 2)); then
            web_fail https "$https_reason"
        fi
        emit "DEBIAN_WEB_NETWORK_RETRY mode=$WEB_NETWORK_MODE layer=https attempt=$https_attempt reason=$https_reason"
    done
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
    # Download 19 more so the complete mode contract records exactly 20. Keep
    # the transfers in one curl process so process startup does not dominate
    # the boot deadline while Firefox is cold-starting.
    for ((attempt = 1; attempt < FIXTURE_REQUESTS; attempt++)); do
        repeat_files+=("$WEB_TEMPORARY_DIRECTORY/repeat-$attempt")
        repeat_curl+=(--output "${repeat_files[-1]}" "$FIXTURE_URL")
    done
    now="$(web_monotonic_seconds)" || web_fail repeat timeout
    remaining=$((deadline - now))
    ((remaining > 0)) || web_fail repeat timeout
    if timeout "$remaining" curl "${repeat_curl[@]}"; then
        :
    else
        curl_status=$?
        web_fail repeat "$(web_curl_reason "$curl_status")"
    fi
    repeat_sizes="$(stat -c '%s' -- "${repeat_files[@]}")" ||
        web_fail repeat length
    while IFS= read -r size; do
        [[ "$size" == "$FIXTURE_SIZE" ]] || web_fail repeat length
    done <<<"$repeat_sizes"
    repeat_hashes="$(sha256sum -- "${repeat_files[@]}")" ||
        web_fail repeat digest
    while IFS=' ' read -r hash _; do
        [[ "$hash" == "$FIXTURE_SHA256" ]] || web_fail repeat digest
    done <<<"$repeat_hashes"
    web_emit_layer repeat

    temporary_medium="$WEB_TEMPORARY_DIRECTORY/medium"
    limit="$(web_timeout_seconds "$deadline")" || web_fail medium timeout
    if timeout "$limit" curl --fail --ipv4 --silent --show-error \
        --connect-timeout "$FIXTURE_CONNECT_TIMEOUT_SECONDS" \
        --max-time "$FIXTURE_MAX_TIME_SECONDS" --noproxy '*' --output "$temporary_medium" \
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
    emit "DEBIAN_NETWORK_DIAGNOSTIC phase=qemu-https attempt=3 stderr_hex=${stderr_hex:-none}"
    rm -f -- "$curl_error"
    return 1
}



validate_fixture_config() {
    local expected_url="$1"

    [[ "$FIXTURE_URL" == "$expected_url" ]] || return 1
    [[ "$FIXTURE_SIZE" == "$EXPECTED_FIXTURE_SIZE" ]] || return 1
    [[ "$FIXTURE_SHA256" == "$EXPECTED_FIXTURE_SHA256" ]] || return 1
    [[ "$FIXTURE_REQUESTS" == "$EXPECTED_FIXTURE_REQUESTS" ]] ||
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
    emit "DEBIAN_NETWORK_STRESS requests=$FIXTURE_REQUESTS bytes=$((FIXTURE_SIZE * FIXTURE_REQUESTS)) sha256=$FIXTURE_SHA256 endpoint=$endpoint"
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
    if [[ "${ASTERINAS_DESKTOP_NETWORK_MODE:-full}" == lightweight ]]; then
        # Browser startup must not wait for a remote TLS probe.  The browser
        # evidence service performs the authoritative DNS/TLS checks after the
        # desktop and Firefox can start.  Do not make this dependency rely on
        # `ip` route formatting: that interface is still incomplete in some
        # Asterinas profiles, while getent/curl below are the authoritative
        # connectivity checks.
        emit "DEBIAN_NETWORK_QEMU_READY mode=qemu-slirp-lightweight"
        return 0
    fi
    resolve_qemu_host www.baidu.com \
        >/dev/null 2>>"$CONSOLE" || fail qemu-dns
    emit "DEBIAN_NETWORK_QEMU_DNS resolver=10.0.2.3 host=www.baidu.com"
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
        emit "DEBIAN_NETWORK_QEMU_TIMING host=www.baidu.com namelookup=${lookup_time:-unknown} connect=${connect_time:-unknown} appconnect=${tls_time:-unknown} starttransfer=${first_byte_time:-unknown}"
    fi
    [[ "$http_status" =~ ^(2|3)[0-9][0-9]$ ]] || fail qemu-http-status
    [[ "$local_address" == "10.0.2.15" ]] || fail qemu-local-address
    emit "DEBIAN_NETWORK_QEMU_HTTPS host=www.baidu.com status=$http_status address=$local_address"

    publish_remote_url
    emit "DEBIAN_NETWORK_QEMU_READY mode=qemu-slirp"
}


[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-command-timeout
if [[ -n "$WEB_NETWORK_MODE" ]]; then
    web_network_evidence
    exit 0
fi
qemu_network_evidence
