#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_BROWSER_WEB_TIMEOUT_SECONDS:-5400}"
readonly FORMAL_TIMEOUT_SECONDS="${ASTERINAS_BROWSER_WEB_FORMAL_TIMEOUT_SECONDS:-1200}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_WEB_PROC_ROOT:-/proc}"
readonly PROFILE=/home/asterinas/.mozilla/asterinas-browser-web
readonly GATE=/usr/lib/asterinas/browser-web-marionette-gate
readonly CURL_LOG=/home/asterinas/browser-web-curl-evidence.log
readonly SECURITY_LOG=/home/asterinas/browser-web-security-evidence.log
readonly FIREFOX_STDERR=/home/asterinas/firefox-web-stderr.log
readonly SYSTEM_CA=/etc/ssl/certs/ca-certificates.crt
readonly TRUST_STATIC_LOG=/usr/share/asterinas/browser-web-trust-static.log
readonly TIMELINE_LOG=/home/asterinas/browser-web-timeline.log
readonly PID_FILE=/home/asterinas/browser-web-firefox.pid
readonly GATE_STDERR=/run/asterinas-browser-web-gate.stderr
readonly GATE_DIAGNOSTIC_LOG=/home/asterinas/browser-web-gate-diagnostic.log
readonly GECKO_PROFILE_DIR=/home/asterinas/Downloads
readonly DETAIL_DIAGNOSTIC_MARKER=/run/asterinas-browser-web-detail-phase
readonly HOT_MAPS_DIAGNOSTIC_MARKER=/run/asterinas-browser-web-hot-maps-captured
readonly HOT_MAP_MAX_LINES=128
readonly NETWORK_ERROR_LOG=/run/asterinas-browser-web-network-error
readonly USER_ID=1000
readonly NETWORK_MODE="${ASTERINAS_WEB_NETWORK_MODE:-}"
readonly BASIC_ONLY="${ASTERINAS_BROWSER_WEB_BASIC_ONLY:-0}"
readonly NETWORK_RESOLVER="${ASTERINAS_WEB_NETWORK_RESOLVER:-}"
readonly PROXY_URL="${ASTERINAS_DESKTOP_PROXY_URL:-}"
readonly PROXY_HOST="${ASTERINAS_DESKTOP_PROXY_HOST:-}"
readonly PROXY_PORT="${ASTERINAS_DESKTOP_PROXY_PORT:-}"
readonly FIXTURE_URL="${ASTERINAS_DESKTOP_FIXTURE_URL:-}"
readonly XORG_LOG=/home/asterinas/Xorg.0.log
readonly SCREENSHOT=/home/asterinas/browser-web-evidence/baidu-search.png
readonly STABILITY_SECONDS=60

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }
fail() { emit "DEBIAN_BROWSER_WEB_FAIL reason=$1"; exit 1; }
systemctl_bounded() {
    /usr/bin/timeout 5 /usr/bin/systemctl "$@"
}
guest_monotonic_ns() {
    local raw seconds fraction ignored
    IFS=' ' read -r raw ignored </proc/uptime
    [[ "$raw" =~ ^([0-9]+)\.([0-9]+)$ ]] || return 1
    seconds="${BASH_REMATCH[1]}"
    fraction="${BASH_REMATCH[2]}000000000"
    printf '%s%s' "$seconds" "${fraction:0:9}"
}
marker() {
    local name="$1" line guest_ns
    guest_ns="$(guest_monotonic_ns)"
    line="A_WEB_TIMELINE marker=$name guest_monotonic_ns=$guest_ns firefox_pid=$browser_pid"
    printf '%s\n' "$line" >>"$TIMELINE_LOG"
    emit "$line"
}

is_canonical_ipv4() {
    local address="$1" octet
    local -a octets
    [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r -a octets <<<"$address"
    for octet in "${octets[@]}"; do
        ((10#$octet <= 255)) || return 1
        [[ "$octet" == 0 || "$octet" != 0* ]] || return 1
    done
}

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$FORMAL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-formal-timeout
[[ "$BASIC_ONLY" == 0 || "$BASIC_ONLY" == 1 ]] || fail invalid-basic-only
case "$NETWORK_MODE" in
    direct)
        [[ -z "$PROXY_URL" && -z "$PROXY_HOST" && -z "$PROXY_PORT" ]] ||
            fail network-mode-proxy-present
        is_canonical_ipv4 "$NETWORK_RESOLVER" || fail network-resolver-invalid
        ;;
    proxy)
        [[ -z "$NETWORK_RESOLVER" ]] || fail network-mode-resolver-present
        is_canonical_ipv4 "$PROXY_HOST" || fail network-proxy-host-invalid
        [[ "$PROXY_PORT" =~ ^[1-9][0-9]{0,4}$ ]] &&
            ((PROXY_PORT <= 65535)) || fail network-proxy-port-invalid
        [[ "$PROXY_URL" == "http://$PROXY_HOST:$PROXY_PORT" ]] ||
            fail network-mode-proxy-invalid
        ;;
    *) fail network-mode-invalid ;;
esac
deadline=$((SECONDS + TIMEOUT_SECONDS))

validate_zero_caps() {
    local pid="$1" role="$2" field
    for field in CapInh CapPrm CapEff CapBnd CapAmb; do
        grep -Eq "^${field}:[[:space:]]+0+$" "$PROC_ROOT/$pid/status" ||
            fail "security-capability-$role-$field"
    done
}

validate_parent_security() {
    local pid="$1" cmdline environment uid target_url child_network_mode
    validate_zero_caps "$pid" parent
    uid="$(sed -n 's/^Uid:[[:space:]]*\([0-9]*\).*/\1/p' "$PROC_ROOT/$pid/status")"
    [[ "$uid" == "$USER_ID" ]] || fail security-parent-uid
    grep -Eq '^NoNewPrivs:[[:space:]]+1$' "$PROC_ROOT/$pid/status" ||
        fail security-parent-no-new-privileges
    cmdline="$(tr '\0' ' ' <"$PROC_ROOT/$pid/cmdline")"
    environment="$(tr '\0' '\n' <"$PROC_ROOT/$pid/environ")"
    [[ "$cmdline" != *--offline* && "$cmdline" != *--no-sandbox* ]] ||
        fail security-parent-forbidden-cmdline
    [[ "$cmdline" == *" --marionette "* ]] ||
        fail security-parent-required-cmdline
    target_url="$(printf '%s\n' "$environment" | sed -n 's/^ASTERINAS_FIREFOX_WEB_TARGET_URL=//p')"
    [[ "$target_url" == "https://www.baidu.com/" ]] ||
        fail security-parent-target-url
    child_network_mode="$(printf '%s\n' "$environment" | sed -n 's/^ASTERINAS_FIREFOX_WEB_NETWORK_MODE=//p')"
    [[ "$child_network_mode" == "$NETWORK_MODE" ]] ||
        fail security-parent-network-mode
    if grep -Eq '^(MOZ_DISABLE_(CONTENT|GMP|RDD|SOCKET)_SANDBOX|MOZ_FORCE_DISABLE_E10S)=([^0]|0*[1-9])' <<<"$environment"; then
        fail security-parent-disable-environment
    fi
    grep -qx 'MOZ_SANDBOX_LOGGING=1' <<<"$environment" ||
        fail security-parent-sandbox-logging
    [[ ! -s "$PROFILE/cert_override.txt" ]] || fail certificate-override-present
    printf 'BROWSER_WEB_SECURITY parent_pid=%s uid=1000 caps=zero nnp=1 sandbox_disable=absent\n' \
        "$pid" >>"$SECURITY_LOG"
    printf 'BROWSER_WEB_NETWORK_ENV parent_pid=%s mode=%s\n' \
        "$pid" "$NETWORK_MODE" >>"$SECURITY_LOG"
}

validate_child_security() {
    local parent="$1" process pid cmdline role seccomp content_seen=false
    CONTENT_SECCOMP_MODE=""
    for process in "$PROC_ROOT"/[1-9]*; do
        [[ -d "$process" ]] || continue
        pid="${process##*/}"
        [[ "$pid" != "$parent" ]] || continue
        cmdline="$(tr '\0' ' ' <"$process/cmdline" 2>/dev/null || true)"
        [[ "$cmdline" == *" -parentPid $parent "* ]] || continue
        role=child
        [[ "$cmdline" == *" socket "* ]] && role=socket
        [[ "$cmdline" == *" rdd "* ]] && role=rdd
        # Firefox 143's RISC-V command line identifies content processes with
        # ``-contentproc`` and no longer carries the older ``tab`` token.
        if [[ "$cmdline" == *" -contentproc "* || "$cmdline" == *" tab "* ]]; then
            role=content
            content_seen=true
        fi
        validate_zero_caps "$pid" "$role"
        grep -Eq '^NoNewPrivs:[[:space:]]+1$' "$process/status" ||
            fail "security-$role-no-new-privileges"
        seccomp="$(sed -n 's/^Seccomp:[[:space:]]*//p' "$process/status")"
        [[ "$seccomp" =~ ^[012]$ ]] || fail "security-$role-seccomp-missing"
        if [[ "$role" == content ]]; then
            [[ "$seccomp" == 0 || "$seccomp" == 2 ]] ||
                fail security-content-seccomp-invalid
            if [[ -z "$CONTENT_SECCOMP_MODE" ]]; then
                CONTENT_SECCOMP_MODE="$seccomp"
            fi
            [[ "$seccomp" == "$CONTENT_SECCOMP_MODE" ]] ||
                fail security-content-seccomp-mixed
        fi
        printf 'BROWSER_WEB_SECURITY child_pid=%s role=%s caps=zero nnp=1 seccomp=%s\n' \
            "$pid" "$role" "$seccomp" >>"$SECURITY_LOG"
    done
    [[ "$content_seen" == true ]] || fail security-content-missing
}

find_firefox_process() {
    local pid
    pid="$(/usr/bin/timeout 2 /usr/bin/cat "$PID_FILE" 2>/dev/null || true)"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s\n' "$pid"
}

validate_firefox_network_profile() {
    local profile_file="$PROFILE/user.js"
    [[ -f "$profile_file" && ! -L "$profile_file" ]] ||
        fail firefox-network-profile-unsafe
    if [[ "$NETWORK_MODE" == proxy ]]; then
        grep -Fxq 'user_pref("network.proxy.type", 1);' "$profile_file" ||
            fail firefox-proxy-type
        grep -Fxq "user_pref(\"network.proxy.http\", \"$PROXY_HOST\");" "$profile_file" ||
            fail firefox-proxy-http
        grep -Fxq "user_pref(\"network.proxy.http_port\", $PROXY_PORT);" "$profile_file" ||
            fail firefox-proxy-http-port
        grep -Fxq "user_pref(\"network.proxy.ssl\", \"$PROXY_HOST\");" "$profile_file" ||
            fail firefox-proxy-ssl
        grep -Fxq "user_pref(\"network.proxy.ssl_port\", $PROXY_PORT);" "$profile_file" ||
            fail firefox-proxy-ssl-port
        grep -Fxq "user_pref(\"network.proxy.no_proxies_on\", \"localhost, 127.0.0.1, $PROXY_HOST\");" "$profile_file" ||
            fail firefox-proxy-no-proxies
        [[ "$(grep -c 'network\.proxy\.' "$profile_file")" == 6 ]] ||
            fail firefox-proxy-profile-extra
    else
        grep -Fxq 'user_pref("network.proxy.type", 0);' "$profile_file" ||
            fail firefox-direct-proxy-type
        [[ "$(grep -c 'network\.proxy\.' "$profile_file")" == 1 ]] ||
            fail firefox-direct-proxy-leak
    fi
}

validate_desktop_input() {
    [[ -c /dev/input/event0 ]] || fail keyboard-event-absent
    [[ -c /dev/input/event1 ]] || fail pointer-event-absent
    grep -q 'Adding extended input device.*Asterinas keyboard' "$XORG_LOG" ||
        fail keyboard-xorg-absent
    grep -q 'Adding extended input device.*Asterinas pointer' "$XORG_LOG" ||
        fail pointer-xorg-absent
}

observe_firefox_stability() {
    local duration="$STABILITY_SECONDS"
    [[ "$BASIC_ONLY" == 1 ]] && duration=5
    local observed_pid end=$((SECONDS + duration))
    while ((SECONDS < end)); do
        kill -0 "$browser_pid" 2>/dev/null || fail firefox-exited-during-stability
        observed_pid="$(find_firefox_process || true)"
        [[ "$observed_pid" == "$browser_pid" ]] ||
            fail firefox-pid-changed-during-stability
        if [[ "$BASIC_ONLY" == 1 ]]; then
            /usr/bin/timeout 3 /usr/bin/sleep 1 ||
                fail firefox-stability-sleep
        else
            /usr/bin/timeout 6 /usr/bin/sleep 5 ||
                fail firefox-stability-sleep
        fi
    done
}

upload_baidu_screenshot() {
    local capture_url signature status
    [[ -f "$SCREENSHOT" && ! -L "$SCREENSHOT" ]] ||
        fail baidu-screenshot-unsafe
    signature="$(head -c 8 -- "$SCREENSHOT" | od -An -v -tx1 | tr -d '[:space:]')"
    [[ "$signature" == 89504e470d0a1a0a ]] || fail baidu-screenshot-png
    [[ "$FIXTURE_URL" == */asterinas-network-probe.bin ]] ||
        fail screenshot-fixture-url
    capture_url="${FIXTURE_URL%/asterinas-network-probe.bin}/browser-quality/capture.png"
    status="$(/usr/bin/timeout 30 curl --fail --silent --show-error \
        --noproxy '*' --max-time 25 --output /dev/null --write-out '%{http_code}' \
        --data-binary "@$SCREENSHOT" "$capture_url")" ||
        fail baidu-screenshot-upload
    [[ "$status" == 201 ]] || fail baidu-screenshot-upload-status
}

validate_dns_and_tls() {
    local hosts name ip line status effective verify lookup connect tls first_byte
    local command_status stderr_hex
    local -a curl_network=()
    if [[ "$BASIC_ONLY" == 1 ]]; then
        hosts=(deb.debian.org)
    else
        hosts=(www.baidu.com www.bilibili.com)
    fi
    if [[ "$NETWORK_MODE" == direct ]]; then
        grep -Fqx "nameserver $NETWORK_RESOLVER" /etc/resolv.conf ||
            fail dns-resolver-mismatch
    else
        curl_network=(--proxy "$PROXY_URL")
    fi
    # Asterinas may expose additional virtual links (for example a host-side
    # helper interface).  The security-relevant contract is that at least one
    # non-loopback link exists and that the verified address/route below use
    # the slirp network.
    local ready=false
    # This is an advisory local-state probe.  Asterinas' `ip` route/address
    # presentation is not yet identical across all profiles, so failure here
    # must not mask the authoritative getent/TLS checks below.  Keep the wait
    # short; curl has its own connect timeout and reports the real outcome.
    for _ in {1..10}; do
        if [[ "$(find /sys/class/net -mindepth 1 -maxdepth 1 ! -name lo | wc -l)" -ge 1 ]] &&
            ip -4 address show | grep -Eq 'inet 10\.0\.2\.[0-9]+/' &&
            ip -4 route show default | grep -Eq '^default via 10\.0\.2\.2 dev '; then
            ready=true
            break
        fi
        /usr/bin/sleep 1
    done
    if [[ "$ready" != true ]]; then
        emit "DEBIAN_BROWSER_WEB_NETWORK_LOCAL state=unverified reason=ip-link-address-route"
    fi
    for name in "${hosts[@]}"; do
        emit "DEBIAN_BROWSER_WEB_NETWORK_PHASE phase=dns host=$name state=start"
        if [[ "$NETWORK_MODE" == proxy ]]; then
            printf 'DNS_DELEGATED mode=proxy host=%s proxy=%s\n' \
                "$name" "$PROXY_URL" >>"$CURL_LOG"
            emit "DEBIAN_BROWSER_WEB_NETWORK_PHASE phase=dns host=$name state=done delegation=proxy"
            continue
        fi
        if hosts="$(/usr/bin/timeout 30 getent ahostsv4 "$name")"; then
            :
        else
            command_status=$?
            emit "DEBIAN_BROWSER_WEB_NETWORK_DIAGNOSTIC phase=dns host=$name status=$command_status"
            fail "dns-$name"
        fi
        ip="$(awk 'NR == 1 { print $1 }' <<<"$hosts")"
        [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || fail "dns-address-$name"
        [[ "$ip" != 127.* && "$ip" != 0.* ]] || fail "dns-loopback-$name"
        printf 'DNS host=%s address=%s\n' "$name" "$ip" >>"$CURL_LOG"
        emit "DEBIAN_BROWSER_WEB_NETWORK_PHASE phase=dns host=$name state=done address=$ip"
    done
    local -a https_names
    if [[ "$BASIC_ONLY" == 1 ]]; then
        https_names=(https://deb.debian.org/)
    else
        https_names=(https://www.baidu.com/ https://www.bilibili.com/)
    fi
    for name in "${https_names[@]}"; do
        : >"$NETWORK_ERROR_LOG" || fail network-error-log-reset
        emit "DEBIAN_BROWSER_WEB_NETWORK_PHASE phase=https host=${name#https://} state=start"
        if line="$(/usr/bin/timeout 135 curl --proto '=https' --tlsv1.2 --fail --location --silent \
            --show-error --connect-timeout 20 --max-time 120 --output /dev/null \
            "${curl_network[@]}" \
            --write-out '%{http_code} %{url_effective} %{ssl_verify_result} %{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer}' \
            "$name" 2>"$NETWORK_ERROR_LOG")"; then
            :
        else
            command_status=$?
            stderr_hex="$(head -c 2048 -- "$NETWORK_ERROR_LOG" | od -An -v -tx1 | tr -d '[:space:]')"
            emit "DEBIAN_BROWSER_WEB_NETWORK_DIAGNOSTIC phase=https host=${name#https://} status=$command_status stderr_hex=${stderr_hex:-none}"
            fail "curl-${name#https://}"
        fi
        read -r status effective verify lookup connect tls first_byte <<<"$line"
        [[ "$status" =~ ^(2|3)[0-9][0-9]$ && "$effective" == https://* && "$verify" == 0 ]] ||
            fail "curl-verification-${name#https://}"
        printf 'HTTPS requested=%s status=%s effective=%s verify=%s\n' \
            "$name" "$status" "$effective" "$verify" >>"$CURL_LOG"
        # Keep the phase timings separate from the acceptance record.  This
        # makes a slow QEMU run distinguish DNS, TCP connect, TLS handshake,
        # and first-byte latency without weakening the strict HTTPS verdict.
        if [[ "$lookup" =~ ^[0-9]+\.[0-9]+$ &&
            "$connect" =~ ^[0-9]+\.[0-9]+$ &&
            "$tls" =~ ^[0-9]+\.[0-9]+$ &&
            "$first_byte" =~ ^[0-9]+\.[0-9]+$ ]]; then
            printf 'HTTPS_TIMING requested=%s namelookup=%s connect=%s appconnect=%s starttransfer=%s\n' \
                "$name" "$lookup" "$connect" "$tls" "$first_byte" >>"$CURL_LOG"
        else
            printf 'HTTPS_TIMING requested=%s namelookup=unknown connect=unknown appconnect=unknown starttransfer=unknown\n' \
                "$name" >>"$CURL_LOG"
        fi
        emit "DEBIAN_BROWSER_WEB_NETWORK_PHASE phase=https host=${name#https://} state=done status=$status"
    done
}

validate_firefox_logs() {
    local log
    for log in "$FIREFOX_STDERR" /home/asterinas/firefox-web-mozilla.log; do
        grep -Fq 'Exiting due to channel error.' "$log" 2>/dev/null &&
            fail firefox-channel-exit
    done
    if grep -Eq 'SCM_RIGHTS.*(EPERM|Operation not permitted)|(EPERM|Operation not permitted).*SCM_RIGHTS' \
        "$FIREFOX_STDERR" /home/asterinas/firefox-web-mozilla.log 2>/dev/null; then
        fail firefox-scm-rights-eperm
    fi
}

[[ -s "$SYSTEM_CA" ]] || fail system-ca-bundle
grep -Eq '^FIREFOX_TRUST_PASS mode=(embedded-xul|system-nss-jit-overlay) ca_certificates=([1-9][0-9]{2,}) firefox=installed ca_package=installed riscv_elf=1 nss_loader=1$' "$TRUST_STATIC_LOG" ||
    fail firefox-trust-static
: >"$CURL_LOG"
: >"$SECURITY_LOG"
printf 'SYSTEM_CA_SHA256 sha256=%s path=%s\n' \
    "$(sha256sum "$SYSTEM_CA" | awk '{print $1}')" "$SYSTEM_CA" >>"$SECURITY_LOG"
printf 'TRUST_STATIC_SHA256 sha256=%s path=%s\n' \
    "$(sha256sum "$TRUST_STATIC_LOG" | awk '{print $1}')" "$TRUST_STATIC_LOG" >>"$SECURITY_LOG"
emit "DEBIAN_BROWSER_WEB_TRUST_STATIC xul_ckbi=audited ca_bundle=audited package_closure=verified"

# Optional bring-up tracer.  It runs only in a separately injected diagnostic
# image and never changes the formal browser security contract.
if [[ "${ASTERINAS_FIREFOX_PTRACE_DIAGNOSTIC:-0}" == 1 ]]; then
    /usr/lib/asterinas/firefox-ptrace-trace &
fi

browser_pid=""
diagnostic_tick=0
emit "DEBIAN_BROWSER_WEB_FIREFOX_WAIT_START"
while ((SECONDS < deadline)); do
    # Prefer the procfs view: systemctl show can block behind a slow systemd
    # manager on Asterinas while the Firefox child is already alive.
    browser_pid="$(find_firefox_process || true)"
    if [[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] &&
        kill -0 "$browser_pid" 2>/dev/null &&
        [[ "$(/usr/bin/timeout 2 /usr/bin/cat "$PROFILE/MarionetteActivePort" 2>/dev/null)" == 2828 ]]; then
        break
    fi
    ((diagnostic_tick += 1))
    if ((diagnostic_tick % 30 == 0)); then
        diagnostic_comm=""
        diagnostic_cmdline=""
        diagnostic_state="missing"
        diagnostic_syscall="none"
        diagnostic_io="none"
        diagnostic_context="none"
        if [[ "$browser_pid" =~ ^[1-9][0-9]*$ ]]; then
            diagnostic_comm="$(/usr/bin/timeout 2 /usr/bin/cat "$PROC_ROOT/$browser_pid/comm" 2>/dev/null || true)"
            diagnostic_cmdline="$(/usr/bin/timeout 2 /usr/bin/tr '\0' ' ' <"$PROC_ROOT/$browser_pid/cmdline" 2>/dev/null | cut -c1-240 || true)"
            diagnostic_state="$(/usr/bin/timeout 2 /usr/bin/awk '/^State:/ {print $2}' "$PROC_ROOT/$browser_pid/status" 2>/dev/null || true)"
            diagnostic_syscall="$(/usr/bin/timeout 2 /usr/bin/cat "$PROC_ROOT/$browser_pid/syscall" 2>/dev/null | cut -c1-180 || true)"
            diagnostic_io="$(/usr/bin/timeout 2 /usr/bin/awk '/^(rchar|read_bytes|syscr):/ {printf "%s=%s,", $1, $2}' "$PROC_ROOT/$browser_pid/io" 2>/dev/null | sed 's/,$//' || true)"
            diagnostic_context="$(/usr/bin/timeout 2 /usr/bin/awk '/^(voluntary_ctxt_switches|nonvoluntary_ctxt_switches):/ {printf "%s=%s,", $1, $2}' "$PROC_ROOT/$browser_pid/status" 2>/dev/null | sed 's/,$//' || true)"
        fi
        emit "DEBIAN_BROWSER_WEB_FIREFOX_DIAGNOSTIC pid=${browser_pid:-none} comm=${diagnostic_comm:-none} state=${diagnostic_state:-none} profile_dir=$(if [[ -d "$PROFILE" ]]; then printf present; else printf absent; fi) marionette=$(if [[ -s "$PROFILE/MarionetteActivePort" ]]; then printf present; else printf absent; fi) syscall=$(printf '%s' "${diagnostic_syscall:-none}" | tr ' ' '_') io=${diagnostic_io:-none} context=${diagnostic_context:-none} cmdline=$(printf '%s' "${diagnostic_cmdline:-none}" | tr ' ' '_')"
    fi
    /usr/bin/timeout 2 /usr/bin/sleep 1 || true
done
[[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] || fail firefox-timeout
marker BOOT_MARIONETTE_PORT_READY
# Do not query the systemd manager on the critical path before the content
# gate.  The PID file, live procfs identity, Marionette endpoint, and security
# checks below already bind the gate to the running Firefox process.  Asterinas
# can transiently stall a systemctl D-Bus round trip while Firefox is CPU-bound;
# the strict NRestarts=0 service check still runs after content evidence exists.
validate_parent_security "$browser_pid"
validate_firefox_network_profile
# Resolve network/DNS/TLS only after Firefox is demonstrably alive.  This
# preserves the strict online checks while ensuring a slow curl cannot hide a
# Firefox startup failure or suppress its bounded diagnostics.
validate_dns_and_tls
if [[ "$NETWORK_MODE" == proxy ]]; then
    emit "DEBIAN_BROWSER_WEB_NETWORK mode=proxy nic=virtio-slirp dns=proxy-delegated https=curl-verified"
else
    emit "DEBIAN_BROWSER_WEB_NETWORK mode=direct nic=virtio-slirp dns=$NETWORK_RESOLVER https=curl-verified"
fi
remaining=$((deadline - SECONDS))
((remaining > 0)) || fail firefox-timeout
((remaining <= FORMAL_TIMEOUT_SECONDS)) || remaining="$FORMAL_TIMEOUT_SECONDS"
emit "DEBIAN_BROWSER_WEB_GATE_START timeout=${remaining}s pid=$browser_pid"

# The content gate can spend a long time inside one Marionette command while
# Firefox is CPU-bound in page JavaScript.  Keep a low-rate, opt-in process
# sample so a timeout distinguishes user-space saturation from a blocked
# kernel syscall without perturbing the normal workload.
gate_sampler_pid=""
start_gate_sampler() {
    [[ "${ASTERINAS_BROWSER_WEB_PROC_DIAGNOSTIC:-0}" == 1 ]] || return 0
    : >"$GATE_DIAGNOSTIC_LOG"
    (
        local tick=0 syscall state io context
        while kill -0 "$browser_pid" 2>/dev/null; do
            syscall="$(/usr/bin/timeout 2 /usr/bin/cat "$PROC_ROOT/$browser_pid/syscall" 2>/dev/null || true)"
            state="$(/usr/bin/timeout 2 /usr/bin/awk '/^State:/ {print $2}' "$PROC_ROOT/$browser_pid/status" 2>/dev/null || true)"
            io="$(/usr/bin/timeout 2 /usr/bin/awk '/^(rchar|read_bytes|syscr):/ {printf "%s=%s,", $1, $2}' "$PROC_ROOT/$browser_pid/io" 2>/dev/null | sed 's/,$//' || true)"
            context="$(/usr/bin/timeout 2 /usr/bin/awk '/^(voluntary_ctxt_switches|nonvoluntary_ctxt_switches):/ {printf "%s=%s,", $1, $2}' "$PROC_ROOT/$browser_pid/status" 2>/dev/null | sed 's/,$//' || true)"
            line="$(printf 'DEBIAN_BROWSER_WEB_GATE_DIAGNOSTIC tick=%s pid=%s state=%s syscall=%s io=%s context=%s' \
                "$tick" "$browser_pid" "${state:-unknown}" \
                "$(printf '%s' "${syscall:-none}" | tr ' ' '_')" \
                "${io:-none}" "${context:-none}")"
            printf '%s\n' "$line" >>"$GATE_DIAGNOSTIC_LOG"
            # The QEMU gate terminates immediately after a fatal marker, so an
            # ext2 writeback cache may not persist the diagnostic file.  Mirror
            # this low-rate line to serial, which the host retains synchronously.
            emit "$line"
            if [[ -s "$DETAIL_DIAGNOSTIC_MARKER" ]] && ((tick % 6 == 0)); then
                local process_snapshot process_line child_count=0
                local candidate_pid candidate_ppid candidate_stat candidate_time candidate_args map_dump
                process_snapshot="$(/usr/bin/timeout 5 /usr/bin/ps \
                    -eo pid=,ppid=,stat=,time=,args= 2>/dev/null || true)"
                while IFS= read -r process_line; do
                    [[ "$process_line" == *" -parentPid $browser_pid "* ]] || continue
                    line="DEBIAN_BROWSER_WEB_CHILD_DIAGNOSTIC tick=$tick process=$(printf '%s' "$process_line" | tr ' ' '_')"
                    printf '%s\n' "$line" >>"$GATE_DIAGNOSTIC_LOG"
                    emit "$line"
                    ((child_count += 1))
                    ((child_count < 32)) || break
                done <<<"$process_snapshot"
                # Capture the address map of the running content process once.
                # Firefox 143 no longer includes the old `tab` token in every
                # content command line.  The runnable child with the largest
                # CPU time is the stable hot-process identity instead; its maps
                # let host-side QEMU GDB PC samples distinguish libxul,
                # JIT/anonymous memory, and kernel execution.
                if [[ ! -e "$HOT_MAPS_DIAGNOSTIC_MARKER" ]]; then
                    local hot_pid="" hot_ppid="" hot_stat="" hot_time="" hot_seconds=-1
                    while IFS= read -r process_line; do
                        [[ "$process_line" == *" -parentPid $browser_pid "* ]] || continue
                        read -r candidate_pid candidate_ppid candidate_stat candidate_time candidate_args <<<"$process_line"
                        [[ "$candidate_pid" =~ ^[1-9][0-9]*$ ]] || continue
                        [[ "$candidate_stat" == R* ]] || continue
                        local candidate_seconds=0
                        if [[ "$candidate_time" =~ ^([0-9]+):([0-9]{2})$ ]]; then
                            candidate_seconds=$((10#${BASH_REMATCH[1]} * 60 + 10#${BASH_REMATCH[2]}))
                        elif [[ "$candidate_time" =~ ^([0-9]+):([0-9]{2}):([0-9]{2})$ ]]; then
                            candidate_seconds=$((10#${BASH_REMATCH[1]} * 3600 + 10#${BASH_REMATCH[2]} * 60 + 10#${BASH_REMATCH[3]}))
                        else
                            continue
                        fi
                        if ((candidate_seconds > hot_seconds)); then
                            hot_pid="$candidate_pid"
                            hot_ppid="$candidate_ppid"
                            hot_stat="$candidate_stat"
                            hot_time="$candidate_time"
                            hot_seconds="$candidate_seconds"
                        fi
                    done <<<"$process_snapshot"
                    if [[ -n "$hot_pid" ]]; then
                        line="DEBIAN_BROWSER_WEB_HOT_PID tick=$tick pid=$hot_pid ppid=$hot_ppid stat=$hot_stat cputime=$hot_time"
                        printf '%s\n' "$line" >>"$GATE_DIAGNOSTIC_LOG"
                        emit "$line"
                        # Procfs maps can contain thousands of VMAs on Firefox.
                        # Read only a small prefix and stream it directly; an
                        # unbounded command-substitution previously caused the
                        # diagnostic path itself to exhaust guest memory.
                        map_dump="/run/asterinas-browser-web-hot-maps"
                        if /usr/bin/timeout 10 /usr/bin/head -n "$HOT_MAP_MAX_LINES" \
                            "$PROC_ROOT/$hot_pid/maps" >"$map_dump" 2>/dev/null &&
                            [[ -s "$map_dump" ]]; then
                            while IFS= read -r line; do
                                line="DEBIAN_BROWSER_WEB_HOT_MAP pid=$hot_pid map=$(printf '%s' "$line" | tr ' ' '_')"
                                printf '%s\n' "$line" >>"$GATE_DIAGNOSTIC_LOG"
                                emit "$line"
                            done <"$map_dump"
                            : >"$HOT_MAPS_DIAGNOSTIC_MARKER"
                            emit "DEBIAN_BROWSER_WEB_HOT_MAP_DONE pid=$hot_pid"
                        else
                            emit "DEBIAN_BROWSER_WEB_HOT_MAP_FAIL pid=$hot_pid"
                        fi
                    fi
                fi
            fi
            ((tick += 1))
            /usr/bin/timeout 7 /usr/bin/sleep 5 || break
        done
    ) &
    gate_sampler_pid=$!
}

capture_gecko_profile() {
    [[ "${ASTERINAS_FIREFOX_GECKO_PROFILE:-0}" == 1 ]] || return 0
    emit "DEBIAN_BROWSER_WEB_GECKO_PROFILE state=signal-stop pid=$browser_pid"
    if ! kill -USR2 "$browser_pid" 2>/dev/null; then
        emit "DEBIAN_BROWSER_WEB_GECKO_PROFILE state=failed reason=sigusr2"
        return 0
    fi
    local profile="" size="" previous_size="" stable=0
    local candidate
    for _ in {1..30}; do
        for candidate in "$GECKO_PROFILE_DIR"/profile_*.json; do
            [[ -f "$candidate" ]] || continue
            profile="$candidate"
        done
        if [[ -n "$profile" ]]; then
            size="$(/usr/bin/stat -c %s "$profile" 2>/dev/null || true)"
            if [[ "$size" =~ ^[1-9][0-9]*$ && "$size" == "$previous_size" ]]; then
                ((stable += 1))
            else
                stable=0
            fi
            previous_size="$size"
            ((stable >= 2)) && break
        fi
        /usr/bin/timeout 2 /usr/bin/sleep 1 || break
    done
    if [[ -z "$profile" || ! "$size" =~ ^[1-9][0-9]*$ ]]; then
        emit "DEBIAN_BROWSER_WEB_GECKO_PROFILE state=failed reason=no-profile"
        return 0
    fi
    /usr/bin/timeout 20 /usr/bin/sync "$profile" || true
    emit "DEBIAN_BROWSER_WEB_GECKO_PROFILE state=ready path=$profile bytes=$size sha256=$(sha256sum "$profile" | awk '{print $1}')"
}
stop_gate_sampler() {
    if [[ -n "$gate_sampler_pid" ]] && kill -0 "$gate_sampler_pid" 2>/dev/null; then
        kill "$gate_sampler_pid" 2>/dev/null || true
        wait "$gate_sampler_pid" 2>/dev/null || true
    fi
}
start_gate_sampler
: >"$GATE_STDERR"
gate_args=(--firefox-pid "$browser_pid" --timeout "$remaining" \
    --evidence-dir /home/asterinas/browser-web-evidence)
if [[ "$BASIC_ONLY" == 1 ]]; then
    gate_args+=(--basic-only)
fi
if ! content="$($GATE "${gate_args[@]}" \
    2> >(/usr/bin/tee -a "$GATE_STDERR" >>"$CONSOLE"))"; then
    stop_gate_sampler
    capture_gecko_profile
    cat "$GATE_STDERR" >>"$TIMELINE_LOG"
    if grep -Fq 'challenge host observed' "$GATE_STDERR"; then
        emit "DEBIAN_BROWSER_WEB_EXTERNAL_BLOCK site=baidu reason=captcha"
    fi
    # Publish the terminal failure before sync.  Asterinas can leave sync in an
    # uninterruptible wait after a browser failure; the host must still be able
    # to classify the result and reclaim QEMU without waiting for the global
    # boot timeout.  The subsequent sync is best-effort preservation of partial
    # page evidence and cannot weaken the serial failure verdict.
    emit "DEBIAN_BROWSER_WEB_FAIL reason=browser-content"
    /usr/bin/timeout 20 /usr/bin/sync || true
    exit 1
fi
stop_gate_sampler
cat "$GATE_STDERR" >>"$TIMELINE_LOG"
if [[ "$BASIC_ONLY" == 1 ]]; then
    [[ "$content" == "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass download=pass public_sites=not-run capabilities=fixture" ]] ||
        fail browser-content-output
else
[[ "$content" == "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass baidu_home=pass baidu_search=observed bilibili_home=pass bilibili_detail=pass bv=BV"*" tls=verified baidu_outcome=pass capabilities=pass download=pass" ||
    "$content" == "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass baidu_home=pass baidu_search=observed bilibili_home=pass bilibili_detail=pass bv=BV"*" tls=verified baidu_outcome=external-captcha capabilities=pass download=pass" ]] ||
    fail browser-content-output
fi
if [[ "$content" == *" baidu_outcome=external-captcha" ]]; then
    emit "DEBIAN_BROWSER_WEB_EXTERNAL_BLOCK site=baidu reason=captcha"
fi
validate_desktop_input
observe_firefox_stability
systemctl_bounded is-active --quiet asterinas-browser-web.service || fail firefox-not-active-after-gate
[[ "$(systemctl_bounded show --property MainPID --value asterinas-browser-web.service 2>/dev/null)" == "$browser_pid" ]] ||
    fail firefox-pid-changed-during-gate
[[ "$(systemctl_bounded show --property NRestarts --value asterinas-browser-web.service 2>/dev/null)" == 0 ]] ||
    fail firefox-restarted-during-gate
validate_firefox_logs
printf 'BROWSER_WEB_SECURITY service_pid=%s nrestarts=0 stable=1 active=1\n' \
    "$browser_pid" >>"$SECURITY_LOG"
validate_child_security "$browser_pid"
if [[ "$BASIC_ONLY" == 0 ]]; then
    upload_baidu_screenshot
fi
emit "DEBIAN_BROWSER_WEB_SECURITY parent_uid=1000 caps=zero nnp=1 content_processes=audited"
case "$CONTENT_SECCOMP_MODE" in
    0)
        firefox_arch="$(uname -m)"
        [[ "$firefox_arch" == riscv64 ]] || fail security-content-seccomp-unexpected-zero
        emit "DEBIAN_BROWSER_WEB_SANDBOX content_seccomp=0 state=unavailable-firefox-riscv64-build"
        ;;
    2)
        emit "DEBIAN_BROWSER_WEB_SANDBOX content_seccomp=2 state=enabled"
        ;;
    *)
        fail security-content-seccomp-missing
        ;;
esac
if [[ "$BASIC_ONLY" == 1 ]]; then
    emit "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass download=pass public_sites=not-run capabilities=fixture"
else
    emit "$content"
fi
emit "DEBIAN_BROWSER_WEB_TLS cert_verify=strict firefox_https=success override=absent"
/usr/bin/timeout 20 /usr/bin/sync || fail evidence-sync
if [[ "$BASIC_ONLY" == 1 ]]; then
    emit "DEBIAN_FIREFOX_BASIC_READY mode=$NETWORK_MODE fixture=pass stable=pass"
else
    emit "DEBIAN_FIREFOX_BAIDU_READY mode=$NETWORK_MODE home=pass logo=pass search=pass input=pass stable=pass screenshot=baidu-search.png"
fi
