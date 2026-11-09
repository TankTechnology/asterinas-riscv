#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Guest-side subsystem selftests. Runs inside the booted guest and prints one
# machine-readable line per case:
#
#     SELFTEST <subsystem> <case> PASS|FAIL|SKIP <detail>
#
# The host collector selects the subsystems from tools/riscv/selftests/catalog.py
# and runs this script once per boot. Keep each case independent and short.

set -u

report() {
    printf 'SELFTEST %s %s %s %s\n' "$1" "$2" "$3" "${4:-}"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# --- console ---------------------------------------------------------------
console_identity() {
    name=$(hostname 2>/dev/null || echo unknown)
    release=$(. /etc/os-release 2>/dev/null && printf '%s %s' "${NAME:-}" "${VERSION_ID:-}")
    if [ -n "$name" ] && [ -n "$release" ]; then
        report console identity PASS "host=$name os=$release"
    else
        report console identity FAIL "missing hostname or /etc/os-release"
    fi
}

console_clock() {
    before=$(date +%s)
    sleep 1
    after=$(date +%s)
    if [ "$after" -le "$before" ]; then
        report console clock FAIL "clock does not advance: $before..$after"
        return
    fi
    if [ -n "${SELFTEST_EXPECT_EPOCH:-}" ]; then
        delta=$((after - SELFTEST_EXPECT_EPOCH))
        if [ "$delta" -lt 0 ]; then delta=$((-delta)); fi
        if [ "$delta" -le 86400 ]; then
            report console clock PASS "epoch=$after delta=${delta}s"
        else
            report console clock FAIL "epoch=$after expected~$SELFTEST_EXPECT_EPOCH delta=${delta}s"
        fi
        return
    fi
    if [ "$after" -gt 1700000000 ]; then
        report console clock PASS "epoch=$after"
    else
        report console clock FAIL "epoch=$before..$after"
    fi
}

console_tty() {
    current=$(tty 2>/dev/null || true)
    if [ -n "$current" ] || [ -c /dev/ttyS0 ]; then
        report console tty PASS "tty=${current:-/dev/ttyS0}"
    else
        report console tty FAIL "no controlling tty"
    fi
}

console_sudo() {
    if [ "$(id -u)" = 0 ]; then
        report console sudo PASS "running as root"
    elif have sudo && sudo -n true 2>/dev/null; then
        report console sudo PASS "passwordless sudo"
    else
        report console sudo FAIL "sudo unavailable"
    fi
}

# --- block -----------------------------------------------------------------
block_rootfs_rw() {
    probe=/var/tmp/asterinas-selftest-write
    if (printf 'ok\n' >"$probe") 2>/dev/null && [ "$(cat "$probe")" = ok ]; then
        rm -f "$probe"
        report block rootfs-rw PASS "root is writable"
    else
        report block rootfs-rw FAIL "cannot write /var/tmp"
    fi
}

block_fsck() {
    report block fsck SKIP "root filesystem is mounted; offline image check owns fsck"
}

block_space() {
    avail=$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "$avail" ] && [ "$avail" -ge 102400 ]; then
        report block space PASS "avail_kb=$avail"
    else
        report block space FAIL "avail_kb=${avail:-unknown}"
    fi
}

# --- net -------------------------------------------------------------------
net_loopback() {
    if have ping && ping -c 1 -W 2 127.0.0.1 >/dev/null 2>&1; then
        report net loopback PASS "ping 127.0.0.1"
    elif have python3 && python3 - <<'EOF' >/dev/null 2>&1
import socket
s = socket.create_connection(("127.0.0.1", 22), timeout=1)
EOF
    then
        report net loopback PASS "tcp connect"
    else
        report net loopback FAIL "no loopback roundtrip"
    fi
}

net_route() {
    if have ip && ip route 2>/dev/null | grep -q '^default '; then
        report net route PASS "default route present"
    elif [ -r /proc/net/route ] &&
        awk 'NR>1 && $2=="00000000" {found=1} END {exit !found}' /proc/net/route; then
        report net route PASS "default route present"
    else
        report net route FAIL "no default route"
    fi
}

net_dns() {
    host=${SELFTEST_DNS_HOST:-www.baidu.com}
    if have getent && getent hosts "$host" >/dev/null 2>&1; then
        report net dns PASS "resolved $host"
    elif have python3 && python3 - "$host" <<'EOF' >/dev/null 2>&1
import socket, sys
socket.getaddrinfo(sys.argv[1], 80)
EOF
    then
        report net dns PASS "resolved $host"
    else
        report net dns FAIL "cannot resolve $host"
    fi
}

net_http() {
    if [ -z "${SELFTEST_HTTP_URL:-}" ]; then
        report net http SKIP "no SELFTEST_HTTP_URL"
        return
    fi
    if ! have curl; then
        report net http SKIP "curl not installed"
        return
    fi
    code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$SELFTEST_HTTP_URL" 2>/dev/null || true)
    if [ "$code" = 200 ]; then
        report net http PASS "http=$code"
    else
        report net http FAIL "http=${code:-none}"
    fi
}

# --- power (terminal cases; run separately) --------------------------------
power_reboot() {
    report power reboot SKIP "terminal case; run explicitly with a reboot harness"
}

power_poweroff() {
    report power poweroff SKIP "terminal case; run explicitly with a poweroff harness"
}

run_subsystem() {
    case "$1" in
        console)
            console_identity
            console_clock
            console_tty
            console_sudo
            ;;
        block)
            block_rootfs_rw
            block_fsck
            block_space
            ;;
        net)
            net_loopback
            net_route
            net_dns
            net_http
            ;;
        power)
            power_reboot
            power_poweroff
            ;;
        *)
            report "$1" unknown FAIL "unknown subsystem"
            ;;
    esac
}

for subsystem in "$@"; do
    run_subsystem "$subsystem"
done

printf 'SELFTEST_DONE\n'
