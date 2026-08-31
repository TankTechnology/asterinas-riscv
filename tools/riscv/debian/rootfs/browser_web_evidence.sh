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
readonly GATE_STDERR=/run/asterinas-browser-web-gate.stderr
readonly USER_ID=1000

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }
fail() { emit "DEBIAN_BROWSER_WEB_FAIL reason=$1"; exit 1; }
marker() {
    local name="$1" line
    line="A_WEB_TIMELINE marker=$name guest_monotonic_ns=$(awk '{printf \"%.0f\", $1 * 1000000000}' /proc/uptime) firefox_pid=$browser_pid"
    printf '%s\n' "$line" >>"$TIMELINE_LOG"
    emit "$line"
}

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$FORMAL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-formal-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))

validate_zero_caps() {
    local pid="$1" role="$2" field
    for field in CapInh CapPrm CapEff CapBnd CapAmb; do
        grep -Eq "^${field}:[[:space:]]+0+$" "$PROC_ROOT/$pid/status" ||
            fail "security-capability-$role-$field"
    done
}

validate_parent_security() {
    local pid="$1" cmdline environment uid
    validate_zero_caps "$pid" parent
    uid="$(sed -n 's/^Uid:[[:space:]]*\([0-9]*\).*/\1/p' "$PROC_ROOT/$pid/status")"
    [[ "$uid" == "$USER_ID" ]] || fail security-parent-uid
    grep -Eq '^NoNewPrivs:[[:space:]]+1$' "$PROC_ROOT/$pid/status" ||
        fail security-parent-no-new-privileges
    cmdline="$(tr '\0' ' ' <"$PROC_ROOT/$pid/cmdline")"
    environment="$(tr '\0' '\n' <"$PROC_ROOT/$pid/environ")"
    [[ "$cmdline" != *--offline* && "$cmdline" != *--no-sandbox* ]] ||
        fail security-parent-forbidden-cmdline
    [[ "$cmdline" == *" --marionette "* && "$cmdline" == *" https://www.baidu.com/"* ]] ||
        fail security-parent-required-cmdline
    if grep -Eq '^(MOZ_DISABLE_(CONTENT|GMP|RDD|SOCKET)_SANDBOX|MOZ_FORCE_DISABLE_E10S)=([^0]|0*[1-9])' <<<"$environment"; then
        fail security-parent-disable-environment
    fi
    grep -qx 'MOZ_SANDBOX_LOGGING=1' <<<"$environment" ||
        fail security-parent-sandbox-logging
    [[ ! -s "$PROFILE/cert_override.txt" ]] || fail certificate-override-present
    printf 'BROWSER_WEB_SECURITY parent_pid=%s uid=1000 caps=zero nnp=1 sandbox_disable=absent\n' \
        "$pid" >>"$SECURITY_LOG"
}

validate_child_security() {
    local parent="$1" process pid cmdline role seccomp content_seen=false
    for process in "$PROC_ROOT"/[1-9]*; do
        [[ -d "$process" ]] || continue
        pid="${process##*/}"
        [[ "$pid" != "$parent" ]] || continue
        cmdline="$(tr '\0' ' ' <"$process/cmdline" 2>/dev/null || true)"
        [[ "$cmdline" == *" -parentPid $parent "* ]] || continue
        role=child
        [[ "$cmdline" == *" socket "* ]] && role=socket
        [[ "$cmdline" == *" rdd "* ]] && role=rdd
        if [[ "$cmdline" == *" tab "* ]]; then role=content; content_seen=true; fi
        validate_zero_caps "$pid" "$role"
        grep -Eq '^NoNewPrivs:[[:space:]]+1$' "$process/status" ||
            fail "security-$role-no-new-privileges"
        seccomp="$(sed -n 's/^Seccomp:[[:space:]]*//p' "$process/status")"
        [[ "$seccomp" =~ ^[012]$ ]] || fail "security-$role-seccomp-missing"
        if [[ "$role" == content ]]; then
            [[ "$seccomp" == 2 ]] || fail security-content-seccomp
        fi
        printf 'BROWSER_WEB_SECURITY child_pid=%s role=%s caps=zero nnp=1 seccomp=%s\n' \
            "$pid" "$role" "$seccomp" >>"$SECURITY_LOG"
    done
    [[ "$content_seen" == true ]] || fail security-content-missing
}

validate_dns_and_tls() {
    local hosts name ip line status effective verify interfaces
    grep -Eq '^nameserver[[:space:]]+10\.0\.2\.3([[:space:]]|$)' /etc/resolv.conf ||
        fail dns-not-slirp-10.0.2.3
    interfaces=""
    local path interface
    for path in /sys/class/net/*; do
        [[ -e "$path" ]] || continue
        interface="${path##*/}"
        [[ "$interface" == lo ]] || interfaces="$interfaces$interface,"
    done
    [[ -n "$interfaces" ]] || fail nic-count
    emit "DEBIAN_BROWSER_WEB_INTERFACES names=$interfaces"
    ip -4 address show | grep -Eq 'inet 10\.0\.2\.[0-9]+/' || fail nic-address
    ip -4 route show default | grep -Eq '^default via 10\.0\.2\.2 dev ' || fail nic-default-route
    for name in www.baidu.com www.bilibili.com; do
        hosts="$(getent ahostsv4 "$name")" || fail "dns-$name"
        ip="$(awk 'NR == 1 { print $1 }' <<<"$hosts")"
        [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || fail "dns-address-$name"
        [[ "$ip" != 127.* && "$ip" != 0.* ]] || fail "dns-loopback-$name"
        printf 'DNS host=%s address=%s\n' "$name" "$ip" >>"$CURL_LOG"
    done
    for name in https://www.baidu.com/ https://www.bilibili.com/; do
        line="$(curl --proto '=https' --tlsv1.2 --fail --location --silent \
            --show-error --max-time 120 --output /dev/null \
            --write-out '%{http_code} %{url_effective} %{ssl_verify_result}' "$name")" ||
            fail "curl-${name#https://}"
        read -r status effective verify <<<"$line"
        [[ "$status" =~ ^(2|3)[0-9][0-9]$ && "$effective" == https://* && "$verify" == 0 ]] ||
            fail "curl-verification-${name#https://}"
        printf 'HTTPS requested=%s status=%s effective=%s verify=%s\n' \
            "$name" "$status" "$effective" "$verify" >>"$CURL_LOG"
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
grep -Eq '^FIREFOX_TRUST_PASS mode=embedded-xul ca_certificates=([1-9][0-9]{2,}) firefox=installed ca_package=installed riscv_elf=1 nss_loader=1$' "$TRUST_STATIC_LOG" ||
    fail firefox-trust-static
: >"$CURL_LOG"
: >"$SECURITY_LOG"
printf 'SYSTEM_CA_SHA256 sha256=%s path=%s\n' \
    "$(sha256sum "$SYSTEM_CA" | awk '{print $1}')" "$SYSTEM_CA" >>"$SECURITY_LOG"
printf 'TRUST_STATIC_SHA256 sha256=%s path=%s\n' \
    "$(sha256sum "$TRUST_STATIC_LOG" | awk '{print $1}')" "$TRUST_STATIC_LOG" >>"$SECURITY_LOG"
validate_dns_and_tls
emit "DEBIAN_BROWSER_WEB_NETWORK nic=virtio-slirp dns=10.0.2.3 https=curl-verified"
emit "DEBIAN_BROWSER_WEB_TRUST_STATIC xul_ckbi=audited ca_bundle=audited package_closure=verified"

browser_pid=""
while ((SECONDS < deadline)); do
    browser_pid="$(systemctl show --property MainPID --value asterinas-browser-web.service 2>/dev/null || true)"
    if [[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] &&
        [[ "$(cat "$PROC_ROOT/$browser_pid/comm" 2>/dev/null)" == firefox-esr ]] &&
        [[ "$(cat "$PROFILE/MarionetteActivePort" 2>/dev/null)" == 2828 ]]; then
        break
    fi
    sleep 1
done
[[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] || fail firefox-timeout
marker BOOT_MARIONETTE_PORT_READY
[[ "$(systemctl show --property NRestarts --value asterinas-browser-web.service 2>/dev/null)" == 0 ]] ||
    fail firefox-restarted-before-gate
validate_parent_security "$browser_pid"
remaining=$((deadline - SECONDS))
((remaining > 0)) || fail firefox-timeout
((remaining <= FORMAL_TIMEOUT_SECONDS)) || remaining="$FORMAL_TIMEOUT_SECONDS"
: >"$GATE_STDERR"
if ! content="$($GATE --firefox-pid "$browser_pid" --timeout "$remaining" \
    --evidence-dir /home/asterinas/browser-web-evidence 2>"$GATE_STDERR")"; then
    cat "$GATE_STDERR" >>"$TIMELINE_LOG"
    cat "$GATE_STDERR" >>"$CONSOLE"
    fail browser-content
fi
cat "$GATE_STDERR" >>"$TIMELINE_LOG"
cat "$GATE_STDERR" >>"$CONSOLE"
[[ "$content" == "DEBIAN_BROWSER_WEB_CONTENT baidu_home=pass baidu_search=pass bilibili_home=pass bilibili_detail=pass bv=BV"*" tls=verified" ]] ||
    fail browser-content-output
systemctl is-active --quiet asterinas-browser-web.service || fail firefox-not-active-after-gate
[[ "$(systemctl show --property MainPID --value asterinas-browser-web.service 2>/dev/null)" == "$browser_pid" ]] ||
    fail firefox-pid-changed-during-gate
[[ "$(systemctl show --property NRestarts --value asterinas-browser-web.service 2>/dev/null)" == 0 ]] ||
    fail firefox-restarted-during-gate
validate_firefox_logs
printf 'BROWSER_WEB_SECURITY service_pid=%s nrestarts=0 stable=1 active=1\n' \
    "$browser_pid" >>"$SECURITY_LOG"
validate_child_security "$browser_pid"
emit "DEBIAN_BROWSER_WEB_SECURITY parent_uid=1000 caps=zero nnp=1 content_seccomp=2 sandbox=normal"
emit "$content"
emit "DEBIAN_BROWSER_WEB_TLS cert_verify=strict firefox_https=success override=absent"
emit "DEBIAN_BROWSER_WEB_READY user=asterinas display=:0"
