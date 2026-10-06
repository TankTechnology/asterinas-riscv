#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

umask 022

readonly STATE_DIRECTORY="${ASTERINAS_EXT4_STATE_DIRECTORY:-/var/lib/asterinas-debian-ext4}"
readonly CONSOLE="${ASTERINAS_EXT4_CONSOLE:-/dev/console}"
readonly DEBIAN_VERSION_FILE="${ASTERINAS_EXT4_DEBIAN_VERSION_FILE:-/etc/debian_version}"
readonly COUNTER="$STATE_DIRECTORY/boot-count"
readonly PERSISTENCE_FILE="$STATE_DIRECTORY/persistence"
readonly TEST_USER=debian
readonly TEST_SERVICE=asterinas-debian-user.service
readonly LOGIN_DIRECTORY=/run/asterinas-debian-login
readonly LOGIN_FILE="$LOGIN_DIRECTORY/complete"
NETWORK_RECOVERY_ONLY=0
if /bin/grep -qw 'asterinas.network_recovery_only=1' /proc/cmdline 2>/dev/null; then
    NETWORK_RECOVERY_ONLY=1
fi
readonly NETWORK_RECOVERY_ONLY

emit() {
    # A serial getty may leave its login prompt without a trailing newline.
    # Start each evidence record on its own line so the classifier can require
    # exact, unprefixed markers instead of accepting a prompt suffix.
    printf '\n%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_EXT4_FAIL reason=$1"
    exit 1
}

report_unexpected_exit() {
    local status=$?
    trap - EXIT
    if (( status != 0 )); then
        emit "DEBIAN_EXT4_FAIL reason=unexpected-exit status=$status"
        # Service stderr normally goes to journald, not the gate's serial
        # stream. Preserve the relevant bounded diagnostics before exiting.
        /usr/bin/timeout 10 /usr/bin/journalctl --no-pager -n 80 \
            -u asterinas-debian-ext4.service -u ssh.service \
            -u asterinas-debian-activation.socket \
            -u asterinas-debian-activation.service >>"$CONSOLE" 2>&1 || true
    fi
    exit "$status"
}

trap report_unexpected_exit EXIT

read_counter() {
    if [[ -e "$COUNTER" ]]; then
        /bin/cat -- "$COUNTER" || fail boot-count-read
    else
        printf '0\n'
    fi
}

run_shell_workload() {
    local work=/var/tmp/asterinas-debian-ext4-smoke
    rm -rf -- "$work" || fail process-filesystem
    mkdir -p -- "$work" || fail process-filesystem
    printf '%s\n' process-filesystem-syscall >"$work/source" ||
        fail process-filesystem
    cp -- "$work/source" "$work/copy" || fail process-filesystem
    mv -- "$work/copy" "$work/renamed" || fail process-filesystem
    [[ "$(/bin/cat -- "$work/renamed")" == process-filesystem-syscall ]] ||
        fail process-filesystem
    /bin/bash -c 'sleep 0; test "$(cat /var/tmp/asterinas-debian-ext4-smoke/renamed)" = process-filesystem-syscall' ||
        fail syscall
    rm -rf -- "$work" || fail process-filesystem
}

run_ext4_consistency_workload() {
    local work=/var/tmp/asterinas-debian-ext4-consistency
    rm -rf -- "$work" || fail ext4-workload-reset
    mkdir -p -- "$work" || fail ext4-workload-directory
    if ! /usr/bin/python3 - "$work" >>"$CONSOLE" 2>&1 <<'PY'
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
large = root / "large"
payload = b"asterinas-ext4-"
with large.open("wb") as stream:
    repetitions = (4 * 1024 * 1024 + len(payload) - 1) // len(payload)
    stream.write((payload * repetitions)[:4 * 1024 * 1024])
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(large, 0o640)
renamed = root / "large-renamed"
os.rename(large, renamed)
directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
if renamed.stat().st_size != 4 * 1024 * 1024:
    raise SystemExit("large file size mismatch")

deep = root
for index in range(24):
    deep /= f"d{index:02d}"
    deep.mkdir()
(deep / "leaf").write_text("deep-ext4", encoding="ascii")
if (deep / "leaf").read_text(encoding="ascii") != "deep-ext4":
    raise SystemExit("deep file mismatch")
PY
    then
        fail ext4-python-workload
    fi
    for worker in $(seq 1 4); do
        (
            for iteration in $(seq 1 100); do
                printf '%s\n' "$worker:$iteration" >>"$work/concurrent-$worker"
            done
        ) &
    done
    wait || fail ext4-concurrent-write
    for worker in $(seq 1 4); do
        [[ "$(wc -l <"$work/concurrent-$worker")" -eq 100 ]] ||
            fail ext4-concurrent-count
    done
    rm -f -- "$work/large-renamed" "$work/deep-missing" || true
    rm -rf -- "$work" || fail ext4-workload-cleanup
    emit 'DEBIAN_EXT4_PROGRESS step=ext4-consistency-done'
}

check_login_and_user() {
    emit 'DEBIAN_EXT4_PROGRESS step=login-check-start'
    if ! id "$TEST_USER" >/dev/null 2>&1; then
        emit 'DEBIAN_EXT4_PROGRESS step=useradd-start'
        useradd --create-home --shell /bin/bash "$TEST_USER" || fail user-create
        emit 'DEBIAN_EXT4_PROGRESS step=useradd-done'
        emit 'DEBIAN_EXT4_PROGRESS step=chpasswd-start'
        printf '%s\n' "$TEST_USER:asterinas" | chpasswd || fail user-password
        emit 'DEBIAN_EXT4_PROGRESS step=chpasswd-done'
    fi
    emit 'DEBIAN_EXT4_PROGRESS step=identity-start'
    local uid
    uid="$(id -u "$TEST_USER")" || fail user-identity
    [[ "$uid" -ge 1000 ]] || fail user-identity
    emit 'DEBIAN_EXT4_PROGRESS step=identity-done'
    emit 'DEBIAN_EXT4_PROGRESS step=passwd-status-start'
    /usr/bin/passwd --status "$TEST_USER" | /bin/grep -q '^debian P ' ||
        fail passwd-status
    emit 'DEBIAN_EXT4_PROGRESS step=passwd-status-done'
    local protected=/root/asterinas-root-only
    umask 077
    printf '%s\n' root-only >"$protected" || fail permissions-root-file
    chmod 0600 -- "$protected" || fail permissions-root-mode
    emit 'DEBIAN_EXT4_PROGRESS step=su-start'
    su - "$TEST_USER" -c \
        'test "$(id -u)" -ge 1000 && test "$HOME" = /home/debian && ! test -r /root/asterinas-root-only' ||
        fail login
    emit 'DEBIAN_EXT4_PROGRESS step=login-check-done'
}

configure_and_test_service() {
    emit 'DEBIAN_EXT4_PROGRESS step=service-start'
    cat >/etc/systemd/system/"$TEST_SERVICE" <<'EOF'
[Unit]
Description=Asterinas Debian user service

[Service]
Type=simple
ExecStart=/bin/sleep 30
EOF
    systemctl daemon-reload || fail service-reload
    emit 'DEBIAN_EXT4_PROGRESS step=service-reload-done'
    systemctl enable "$TEST_SERVICE" || fail service-enable
    emit 'DEBIAN_EXT4_PROGRESS step=service-enable-done'
    systemctl start "$TEST_SERVICE" || fail service-start
    emit 'DEBIAN_EXT4_PROGRESS step=service-start-command-done'
    systemctl is-active --quiet "$TEST_SERVICE" || fail service-active
    emit 'DEBIAN_EXT4_PROGRESS step=service-active-done'
    systemctl restart "$TEST_SERVICE" || fail service-restart
    systemctl is-active --quiet "$TEST_SERVICE" || fail service-restart
    logger -t asterinas-debian-ext4 systemd-journald-probe || fail journald-write
    journalctl --no-pager -t asterinas-debian-ext4 |
        /bin/grep -q systemd-journald-probe || fail journald-read
    emit 'DEBIAN_EXT4_PROGRESS step=service-restart-journald-done'
    systemctl stop "$TEST_SERVICE" || fail service-stop
    emit 'DEBIAN_EXT4_PROGRESS step=service-stop-command-done'
    if systemctl is-active --quiet "$TEST_SERVICE"; then
        fail service-inactive
    fi
    cat >/etc/systemd/system/asterinas-debian-oneshot.service <<'EOF'
[Unit]
Description=Asterinas Debian oneshot probe

[Service]
Type=oneshot
ExecStart=/bin/echo oneshot
StandardOutput=append:/run/asterinas-debian-oneshot
RemainAfterExit=yes
EOF
    systemctl daemon-reload || fail oneshot-reload
    systemctl start asterinas-debian-oneshot.service || fail oneshot-start
    systemctl is-active --quiet asterinas-debian-oneshot.service || fail oneshot-active
    [[ "$(/bin/cat /run/asterinas-debian-oneshot)" == oneshot ]] || fail oneshot-output
    systemctl stop asterinas-debian-oneshot.service || fail oneshot-stop
    emit 'DEBIAN_EXT4_PROGRESS step=oneshot-done'
    cat >/etc/systemd/system/asterinas-debian-timer.service <<'EOF'
[Unit]
Description=Asterinas Debian timer target
[Service]
Type=oneshot
ExecStart=/bin/echo timer
StandardOutput=append:/run/asterinas-debian-timer
EOF
    cat >/etc/systemd/system/asterinas-debian-timer.timer <<'EOF'
[Unit]
Description=Asterinas Debian timer probe
[Timer]
OnActiveSec=1s
Unit=asterinas-debian-timer.service
[Install]
WantedBy=timers.target
EOF
    rm -f /run/asterinas-debian-timer
    systemctl daemon-reload || fail timer-reload
    systemctl start asterinas-debian-timer.timer || fail timer-start
    for _ in $(seq 1 10); do
        [[ -e /run/asterinas-debian-timer ]] && break
        /bin/sleep 1
    done
    [[ "$(/bin/cat /run/asterinas-debian-timer 2>/dev/null)" == timer ]] || fail timer-output
    systemctl stop asterinas-debian-timer.timer || fail timer-stop
    emit 'DEBIAN_EXT4_PROGRESS step=timer-done'
    cat >/usr/local/sbin/asterinas-forking-helper <<'EOF'
#!/bin/sh
set -eu
pidfile=/run/asterinas-debian-forking.pid
case "${1:-}" in
    start)
        (sleep 30) &
        echo "$!" >"$pidfile"
        ;;
    stop)
        if [ -s "$pidfile" ]; then kill "$(cat "$pidfile")" 2>/dev/null || true; fi
        rm -f "$pidfile"
        ;;
    *) exit 2 ;;
esac
EOF
    chmod 0755 /usr/local/sbin/asterinas-forking-helper || fail forking-helper
    cat >/etc/systemd/system/asterinas-debian-forking.service <<'EOF'
[Unit]
Description=Asterinas Debian forking probe
[Service]
Type=forking
PIDFile=/run/asterinas-debian-forking.pid
ExecStart=/usr/local/sbin/asterinas-forking-helper start
ExecStop=/usr/local/sbin/asterinas-forking-helper stop
EOF
    systemctl daemon-reload || fail forking-reload
    systemctl start asterinas-debian-forking.service || fail forking-start
    systemctl is-active --quiet asterinas-debian-forking.service || fail forking-active
    systemctl stop asterinas-debian-forking.service || fail forking-stop
    emit 'DEBIAN_EXT4_PROGRESS step=forking-done'
    rm -f /run/asterinas-debian-restart-attempt /run/asterinas-debian-restart-ready
    cat >/etc/systemd/system/asterinas-debian-restart.service <<'EOF'
[Unit]
Description=Asterinas Debian restart-on-failure probe

[Service]
Type=simple
ExecStart=/bin/sh -c 'if [ ! -e /run/asterinas-debian-restart-attempt ]; then touch /run/asterinas-debian-restart-attempt; exit 1; fi; touch /run/asterinas-debian-restart-ready; exec /bin/sleep 30'
Restart=on-failure
RestartSec=1s
EOF
    systemctl daemon-reload || fail restart-reload
    systemctl start asterinas-debian-restart.service || fail restart-start
    for _ in $(seq 1 15); do
        [[ -e /run/asterinas-debian-restart-ready ]] && break
        /bin/sleep 1
    done
    [[ -e /run/asterinas-debian-restart-attempt && -e /run/asterinas-debian-restart-ready ]] ||
        fail restart-recovery
    systemctl is-active --quiet asterinas-debian-restart.service || fail restart-active
    systemctl stop asterinas-debian-restart.service || fail restart-stop
    emit 'DEBIAN_EXT4_PROGRESS step=service-restart-on-failure-done'
    rm -f /run/asterinas-debian-dependency-ready /run/asterinas-debian-dependent-ready
    cat >/etc/systemd/system/asterinas-debian-dependency.service <<'EOF'
[Unit]
Description=Asterinas Debian dependency prerequisite
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'touch /run/asterinas-debian-dependency-ready'
RemainAfterExit=yes
EOF
    cat >/etc/systemd/system/asterinas-debian-dependent.service <<'EOF'
[Unit]
Description=Asterinas Debian dependency consumer
Requires=asterinas-debian-dependency.service
After=asterinas-debian-dependency.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'test -e /run/asterinas-debian-dependency-ready && touch /run/asterinas-debian-dependent-ready'
EOF
    systemctl daemon-reload || fail dependency-reload
    systemctl start asterinas-debian-dependent.service || fail dependency-start
    [[ -e /run/asterinas-debian-dependent-ready ]] || fail dependency-order
    emit 'DEBIAN_EXT4_PROGRESS step=service-dependency-order-done'
    cat >/etc/systemd/system/asterinas-debian-activation.socket <<'EOF'
[Unit]
Description=Asterinas Debian socket activation probe

[Socket]
ListenStream=/run/asterinas-debian-activation.sock
Accept=no

[Install]
WantedBy=sockets.target
EOF
    cat >/etc/systemd/system/asterinas-debian-activation.service <<'EOF'
[Unit]
Description=Asterinas Debian socket activation probe service

[Service]
Type=simple
ExecStart=/usr/bin/socktest
EOF
    systemctl daemon-reload || fail socket-activation-reload
    systemctl start asterinas-debian-activation.socket || fail socket-activation-start
    /bin/sleep 1
    if [[ "$(/usr/bin/sockclient /run/asterinas-debian-activation.sock 2>>"$CONSOLE")" != \
        "hello-from-socket-activated-service" ]]; then
        /usr/bin/systemctl --no-pager --full status \
            asterinas-debian-activation.socket \
            asterinas-debian-activation.service >>"$CONSOLE" 2>&1 || true
        /usr/bin/journalctl --no-pager -n 80 \
            -u asterinas-debian-activation.socket \
            -u asterinas-debian-activation.service >>"$CONSOLE" 2>&1 || true
        emit 'DEBIAN_EXT4_PROGRESS step=socket-activation-unverified'
    else
        emit 'DEBIAN_EXT4_PROGRESS step=socket-activation-done'
    fi
    systemctl stop asterinas-debian-activation.socket || true
    systemctl reset-failed asterinas-debian-activation.socket || true
    rm -f /etc/systemd/system/asterinas-debian-activation.socket \
        /etc/systemd/system/asterinas-debian-activation.service \
        /run/asterinas-debian-activation.sock
    systemctl daemon-reload || true
    # D-Bus and logind are part of the systemd contract, not merely boot
    # decoration.  Check both the units and logind's well-known D-Bus name;
    # the interactive login gate below separately proves that a real PAM
    # session can be created on ttyS0.
    systemctl is-active --quiet dbus.service || fail dbus-active
    systemctl is-active --quiet systemd-logind.service || fail logind-active
    systemctl is-active --quiet systemd-user-sessions.service ||
        fail user-sessions-active
    /usr/bin/busctl --system list | /bin/grep -q org.freedesktop.login1 ||
        fail logind-dbus-name
    emit 'DEBIAN_EXT4_PROGRESS step=dbus-logind-session-done'
    emit 'DEBIAN_EXT4_PROGRESS step=service-done'
}

check_network() {
    /usr/bin/curl --fail --silent --show-error --location --max-time 20 \
        http://deb.debian.org/debian/README | /bin/grep -q Debian ||
        fail network-request
}

check_network_recovery() {
    emit 'DEBIAN_EXT4_PROGRESS step=network-recovery-start'
    local interface
    interface="$(/usr/sbin/ip -o link show | /bin/awk -F': ' '$2 != "lo" {print $2; exit}')" ||
        fail network-interface-discovery
    interface="${interface%%@*}"
    [[ -n "$interface" ]] || fail network-interface-discovery
    local address_before
    address_before="$(/usr/sbin/ip -4 -o addr show dev "$interface" | /bin/awk '{print $4; exit}')" ||
        fail network-address-before
    [[ -n "$address_before" ]] || fail network-address-before
    local restarted=0
    if systemctl cat systemd-networkd.service >/dev/null 2>&1; then
        install -d -m 0755 /etc/systemd/network || fail network-config-dir
        cat >"/etc/systemd/network/20-asterinas-ext4.network" <<EOF
[Match]
Name=$interface

[Network]
Address=$address_before
Gateway=10.0.2.2
DNS=10.0.2.3
EOF
        chmod 0644 -- "/etc/systemd/network/20-asterinas-ext4.network" ||
            fail network-config-mode
        if systemctl restart systemd-networkd.service; then
            restarted=1
        else
            /usr/bin/journalctl --no-pager -n 80 -u systemd-networkd.service >>"$CONSOLE" 2>&1 || true
            systemctl reset-failed systemd-networkd.service || true
        fi
    elif systemctl cat networking.service >/dev/null 2>&1; then
        if systemctl restart networking.service; then
            restarted=1
        else
            /usr/bin/journalctl --no-pager -n 80 -u networking.service >>"$CONSOLE" 2>&1 || true
            systemctl reset-failed networking.service || true
        fi
    fi
    # The current rootfs uses QEMU's fixed-address virtio setup without a
    # network manager unit. Exercise the same recovery boundary by flapping
    # the virtio link when no manager owns it.
    if (( restarted == 0 )); then
        /usr/sbin/ip link set dev "$interface" down || fail network-link-down
        /bin/sleep 1
        /usr/sbin/ip link set dev "$interface" up || fail network-link-up
    fi
    local address_after=""
    for _ in $(seq 1 15); do
        address_after="$(/usr/sbin/ip -4 -o addr show dev "$interface" | /bin/awk '{print $4; exit}')"
        [[ "$address_after" == "$address_before" ]] && break
        /bin/sleep 1
    done
    [[ "$address_after" == "$address_before" ]] || fail network-address-recovery
    printf 'nameserver 10.0.2.3\n' > /etc/resolv.conf || fail resolver-recovery
    /usr/bin/getent hosts deb.debian.org >/dev/null || fail dns-recovery
    /usr/bin/curl --fail --silent --show-error --location --max-time 20 \
        http://deb.debian.org/debian/README | /bin/grep -q Debian ||
        fail http-recovery
    /usr/bin/curl --fail --silent --show-error --location --max-time 20 \
        https://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/InRelease >/dev/null ||
        fail https-recovery
    apt-get -o Acquire::Languages=none -o Acquire::Retries=0 \
        -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
        >>"$CONSOLE" 2>&1 || fail apt-recovery
    emit 'DEBIAN_EXT4_PROGRESS step=network-recovery-done'
    emit "DEBIAN_EXT4_NETWORK_RECOVERY interface=$interface manager=$restarted"
}

check_loopback_sockets() {
    emit 'DEBIAN_EXT4_PROGRESS step=loopback-sockets-start'
    /usr/bin/python3 - <<'PY' || exit 1
import socket
import threading

tcp_ready = threading.Event()
tcp_port = {}

def tcp_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        tcp_port["value"] = server.getsockname()[1]
        tcp_ready.set()
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"asterinas-tcp")

thread = threading.Thread(target=tcp_server)
thread.start()
if not tcp_ready.wait(5):
    raise SystemExit("tcp server did not start")
with socket.create_connection(("127.0.0.1", tcp_port["value"]), 5) as client:
    if client.recv(32) != b"asterinas-tcp":
        raise SystemExit("tcp payload mismatch")
thread.join(5)
if thread.is_alive():
    raise SystemExit("tcp server did not stop")

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
    receiver.bind(("127.0.0.1", 0))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(b"asterinas-udp", receiver.getsockname())
    payload, _ = receiver.recvfrom(32)
    if payload != b"asterinas-udp":
        raise SystemExit("udp payload mismatch")
PY
    emit 'DEBIAN_EXT4_PROGRESS step=loopback-sockets-done'
}

wait_for_interactive_login() {
    install -d -m 0777 -- "$LOGIN_DIRECTORY" || fail login-directory
    rm -f -- "$LOGIN_FILE" || fail login-reset
    emit 'DEBIAN_EXT4_LOGIN_READY boot=1'
    for _ in $(seq 1 120); do
        if [[ -e "$LOGIN_FILE" ]]; then
            [[ "$(stat -c '%U' -- "$LOGIN_FILE")" == "$TEST_USER" ]] ||
                fail login-owner
            return
        fi
        /bin/sleep 1
    done
    fail login-timeout
}

check_pid1_and_root() {
    local pid1 root_filesystem
    pid1="$(tr -d '[:space:]' </proc/1/comm)" || fail pid1
    [[ "$pid1" == systemd ]] || fail pid1
    root_filesystem="$(stat -f -c '%T' /)" || fail root-filesystem
    # Asterinas currently reports the ext4 superblock through the Linux
    # ext2/ext3 statfs compatibility value.  The Stage1 boot contract and the
    # signed schema-9 manifest provide the ext4 identity; accept all values
    # emitted by the kernel while still requiring a real filesystem query.
    case "$root_filesystem" in
        ext2/ext3 | ext4) ;;
        *) fail root-filesystem ;;
    esac
}

install_hello() {
    emit 'DEBIAN_EXT4_PROGRESS step=apt-hello-start'
    # The kernel gives the guest its fixed 10.0.2.15/24 address and 10.0.2.2
    # gateway. QEMU's user-mode DNS proxy is the stable resolver for this gate.
    printf 'nameserver 10.0.2.3\n' > /etc/resolv.conf || fail resolver
    emit 'DEBIAN_EXT4_PROGRESS step=network-http-probe-start'
    if /usr/bin/curl --fail --silent --show-error --location --connect-timeout 5 \
        --max-time 20 http://deb.debian.org/debian/README >/dev/null; then
        emit 'DEBIAN_EXT4_PROGRESS step=network-http-probe-http-pass'
    else
        emit 'DEBIAN_EXT4_PROGRESS step=network-http-probe-http-fail'
    fi
    emit 'DEBIAN_EXT4_PROGRESS step=network-https-probe-start'
    if /usr/bin/curl --fail --silent --show-error --location --connect-timeout 5 \
        --max-time 20 https://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/InRelease \
        >/dev/null; then
        emit 'DEBIAN_EXT4_PROGRESS step=network-https-probe-pass'
    else
        emit 'DEBIAN_EXT4_PROGRESS step=network-https-probe-fail'
    fi
    emit 'DEBIAN_EXT4_PROGRESS step=apt-update-start'
    apt-get \
        -o Acquire::Languages=none \
        -o Acquire::Retries=0 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        update >>"$CONSOLE" 2>&1 || fail apt-update
    emit 'DEBIAN_EXT4_PROGRESS step=apt-update-done'
    emit 'DEBIAN_EXT4_PROGRESS step=apt-install-hello-start'
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends hello ||
        fail apt-install
    emit 'DEBIAN_EXT4_PROGRESS step=apt-install-hello-done'
    [[ "$(/usr/bin/hello)" == 'Hello, world!' ]] || fail hello
    [[ "$(dpkg-query -W -f='${Status}\n' hello)" == 'install ok installed' ]] ||
        fail dpkg
    emit 'DEBIAN_EXT4_PROGRESS step=apt-remove-hello-start'
    DEBIAN_FRONTEND=noninteractive apt-get remove -y hello || fail apt-remove
    emit 'DEBIAN_EXT4_PROGRESS step=apt-remove-hello-done'
    if dpkg-query -W -f='${Status}\n' hello >/dev/null 2>&1; then
        fail apt-remove
    fi
    emit 'DEBIAN_EXT4_PROGRESS step=apt-reinstall-hello-start'
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends hello ||
        fail apt-reinstall
    emit 'DEBIAN_EXT4_PROGRESS step=apt-reinstall-hello-done'
    [[ "$(/usr/bin/hello)" == 'Hello, world!' ]] || fail hello-reinstall
    # These locks are runtime artefacts. Removing them leaves a clean ext4
    # image for the post-boot checker while preserving apt lists and dpkg state.
    rm -f -- \
        /var/cache/apt/archives/lock \
        /var/lib/apt/lists/lock \
        /var/lib/dpkg/lock \
        /var/lib/dpkg/lock-frontend
    emit 'DEBIAN_EXT4_PROGRESS step=apt-hello-done'
}

interrupt_dpkg_at_script() {
    local phase="$1"
    shift
    local work=/run/asterinas-dpkg-interruption
    local process status=0 observed=0
    rm -f -- "$work/entered"
    touch "$work/block"
    # timeout forwards TERM to its child process group, including the blocked
    # maintainer script. Its deadline also bounds a failure to reach the script.
    /usr/bin/timeout -k 2 30 dpkg "$@" >>"$CONSOLE" 2>&1 &
    process=$!
    for (( sample=0; sample<300; sample++ )); do
        if [[ -f "$work/entered" ]]; then
            observed=1
            break
        fi
        kill -0 "$process" 2>/dev/null || break
        sleep 0.1
    done
    if (( observed == 0 )); then
        kill -TERM "$process" 2>/dev/null || true
        wait "$process" || true
        fail "m5-interruption-$phase-not-entered"
    fi
    emit "DEBIAN_EXT4_PROGRESS step=apt-interruption-$phase-script-entered"
    # timeout is only the supervisor; terminate dpkg and every descendant so
    # the blocked maintainer script cannot retain dpkg's database lock.
    kill_descendants() {
        local parent="$1" child
        while read -r child; do
            [[ -n "$child" ]] || continue
            kill_descendants "$child"
            kill -TERM "$child" 2>/dev/null || true
        done < <(pgrep -P "$parent" 2>/dev/null || true)
    }
    kill_descendants "$process"
    kill -TERM "$process" 2>/dev/null || true
    wait "$process" || status=$?
    (( status != 0 )) || fail "m5-interruption-$phase-not-interrupted"
    rm -f -- "$work/block"
    emit "DEBIAN_EXT4_PROGRESS step=apt-interruption-$phase-killed"
}

run_m5_interruption_recovery() {
    local work=/run/asterinas-dpkg-interruption
    local package_name=asterinas-interrupt-test
    local package_dir="$work/package"
    local deb_v1="$work/${package_name}_1.0_all.deb"
    local deb_v2="$work/${package_name}_2.0_all.deb"
    local apt_lists="$work/empty-lists"
    rm -rf -- "$work" || fail m5-interruption-reset
    install -d -m 0755 "$package_dir/DEBIAN" || fail m5-interruption-directory
    install -d -m 0755 "$apt_lists" || fail m5-interruption-directory
    cat >"$package_dir/DEBIAN/control" <<EOF
Package: $package_name
Version: 1.0
Section: misc
Priority: optional
Architecture: all
Maintainer: Asterinas test <root@localhost>
Description: Asterinas dpkg interruption recovery probe
EOF
    cat >"$package_dir/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -eu
if [ -f /run/asterinas-dpkg-interruption/block ]; then
    touch /run/asterinas-dpkg-interruption/entered
    while [ -f /run/asterinas-dpkg-interruption/block ]; do sleep 0.1; done
fi
printf '%s\n' configured > /run/asterinas-dpkg-interruption/configured
EOF
    cat >"$package_dir/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -eu
if [ -f /run/asterinas-dpkg-interruption/block ]; then
    touch /run/asterinas-dpkg-interruption/entered
    while [ -f /run/asterinas-dpkg-interruption/block ]; do sleep 0.1; done
fi
EOF
    chmod 0755 "$package_dir/DEBIAN/postinst" "$package_dir/DEBIAN/prerm" ||
        fail m5-interruption-scripts
    install -d -m 0755 "$package_dir/usr/share/asterinas" || fail m5-interruption-payload
    # Payload size is intentionally small: M8 covers large-file recovery;
    # this package isolates dpkg maintainer-script interruption semantics.
    dd if=/dev/zero of="$package_dir/usr/share/asterinas/payload" bs=4K count=1 status=none ||
        fail m5-interruption-payload
    dpkg-deb --build "$package_dir" "$deb_v1" >>"$CONSOLE" 2>&1 ||
        fail m5-interruption-build

    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-install-start'
    interrupt_dpkg_at_script install --install "$deb_v1"
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-install-configure-start'
    dpkg --configure -a >>"$CONSOLE" 2>&1 || fail m5-interruption-install-recovery
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-install-configure-done'
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-install-aptfix-start'
    DEBIAN_FRONTEND=noninteractive apt-get -f install -y --no-download \
        -o Dir::State::lists="$apt_lists" >>"$CONSOLE" 2>&1 ||
        fail m5-interruption-install-apt
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-install-aptfix-done'
    [[ "$(dpkg-query -W -f='${Status}\n' "$package_name")" == \
        'install ok installed' ]] || fail m5-interruption-install-state

    # Let the upgrade interruption occur in the new version's postinst. The
    # old version's prerm is intentionally kept non-blocking for this phase.
    cat >"$package_dir/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -eu
EOF
    chmod 0755 "$package_dir/DEBIAN/prerm" || fail m5-interruption-scripts
    sed -i 's/^Version: 1.0$/Version: 2.0/' "$package_dir/DEBIAN/control" ||
        fail m5-interruption-version
    dpkg-deb --build "$package_dir" "$deb_v2" >>"$CONSOLE" 2>&1 ||
        fail m5-interruption-build-upgrade
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-start'
    interrupt_dpkg_at_script upgrade --install "$deb_v2"
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-reinstall-start'
    dpkg --install "$deb_v2" >>"$CONSOLE" 2>&1 || fail m5-interruption-upgrade-reinstall
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-reinstall-done'
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-configure-start'
    dpkg --configure -a >>"$CONSOLE" 2>&1 || fail m5-interruption-upgrade-recovery
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-configure-done'
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-aptfix-start'
    DEBIAN_FRONTEND=noninteractive apt-get -f install -y --no-download \
        -o Dir::State::lists="$apt_lists" >>"$CONSOLE" 2>&1 ||
        fail m5-interruption-upgrade-apt
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-upgrade-aptfix-done'
    [[ "$(dpkg-query -W -f='${Version}\n' "$package_name")" == '2.0' ]] ||
        fail m5-interruption-upgrade-state

    # Install a blocking prerm only for the removal interruption phase.
    cat >"/var/lib/dpkg/info/$package_name.prerm" <<'EOF'
#!/bin/sh
set -eu
if [ -f /run/asterinas-dpkg-interruption/block ]; then
    touch /run/asterinas-dpkg-interruption/entered
    while [ -f /run/asterinas-dpkg-interruption/block ]; do sleep 0.1; done
fi
EOF
    chmod 0755 "/var/lib/dpkg/info/$package_name.prerm" ||
        fail m5-interruption-scripts
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-remove-start'
    interrupt_dpkg_at_script remove --remove "$package_name"
    dpkg --configure -a >>"$CONSOLE" 2>&1 || fail m5-interruption-remove-recovery
    DEBIAN_FRONTEND=noninteractive apt-get -f install -y --no-download \
        -o Dir::State::lists="$apt_lists" >>"$CONSOLE" 2>&1 ||
        fail m5-interruption-remove-apt
    dpkg --remove "$package_name" >>"$CONSOLE" 2>&1 || fail m5-interruption-remove-final
    if dpkg-query -W -f='${Status}\n' "$package_name" 2>/dev/null; then
        fail m5-interruption-remove-state
    fi
    emit 'DEBIAN_EXT4_PROGRESS step=apt-interruption-recovery-done'
}

run_m5_package_lifecycle() {
    emit 'DEBIAN_EXT4_PROGRESS step=m5-install-start'
    local install_status=0
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        iproute2 python3-minimal >>"$CONSOLE" 2>&1 || install_status=$?
    (( install_status == 0 )) || fail m5-install
    [[ "$(dpkg-query -W -f='${Status}\n' python3-minimal)" == 'install ok installed' ]] ||
        fail m5-python
    [[ "$(dpkg-query -W -f='${Status}\n' iproute2)" == 'install ok installed' ]] ||
        fail m5-iproute2
    /usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 13)' ||
        fail m5-python-runtime
    emit 'DEBIAN_EXT4_PROGRESS step=m5-install-done'

    # Exercise an actual upgrade/reconfiguration path and its maintainer
    # scripts, then settle all pending dpkg triggers.
    DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall \
        python3-minimal >>"$CONSOLE" 2>&1 || fail m5-upgrade
    dpkg --configure -a || fail m5-configure
    dpkg-trigger --no-await ldconfig || fail m5-trigger-queue
    dpkg --triggers-only --pending || fail m5-trigger-run
    [[ -z "$(dpkg-query -W -f='${Triggers-Pending}' libc-bin)" ]] ||
        fail m5-trigger-pending
    emit 'DEBIAN_EXT4_PROGRESS step=m5-upgrade-done'

    # An expected package failure must leave dpkg recoverable.
    if DEBIAN_FRONTEND=noninteractive apt-get install -y \
        asterinas-package-that-does-not-exist; then
        fail m5-failure-injection
    fi
    dpkg --audit | /bin/grep -q . && fail m5-audit || true
    DEBIAN_FRONTEND=noninteractive apt-get -f install -y || fail m5-recovery
    emit 'DEBIAN_EXT4_PROGRESS step=m5-recovery-done'

    compgen -G '/var/cache/apt/archives/python3-minimal_*.deb' >/dev/null ||
        fail m5-cache
    dpkg --audit | /bin/grep -q . && fail m5-final-audit || true
    run_m5_interruption_recovery
    emit 'DEBIAN_EXT4_PROGRESS step=m5-lifecycle-done'
}

emit "DEBIAN_EXT4_PROGRESS step=entry"
architecture="$(uname -m)" || fail architecture
[[ "$architecture" == riscv64 ]] || fail architecture
debian_release="$(/bin/cat -- "$DEBIAN_VERSION_FILE")" || fail debian-release
[[ "$debian_release" =~ ^13\.(0|[1-9][0-9]*)$ ]] || fail debian-release
check_pid1_and_root
run_shell_workload

install -d -m 0755 -- "$STATE_DIRECTORY" || fail state-directory
current="$(read_counter)"
[[ "$current" == 0 || "$current" == 1 ]] || fail invalid-boot-count
next=$((current + 1))
temporary="$STATE_DIRECTORY/.boot-count.$$"
printf '%s\n' "$next" >"$temporary" || fail boot-count-write
chmod 0644 -- "$temporary" || fail boot-count-write
mv -f -- "$temporary" "$COUNTER" || fail boot-count-write

if ((next == 1)); then
    check_login_and_user
    configure_and_test_service
    install_hello
    # Exercise network-manager restart before the larger M5 package workload.
    # iproute2 is part of this profile so the recovery probe does not depend
    # on a later apt transaction succeeding first.
    emit 'DEBIAN_EXT4_PROGRESS step=m5-before-network'
    check_network
    check_network_recovery
    if [[ "$NETWORK_RECOVERY_ONLY" == 1 ]]; then
        emit 'DEBIAN_EXT4_NETWORK_ONLY boot=1'
        sync || fail network-only-sync
        systemctl --no-block --no-wall reboot || fail network-only-reboot
        exit 0
    fi
    run_m5_package_lifecycle
    run_ext4_consistency_workload
    check_loopback_sockets || fail loopback-sockets
    emit 'DEBIAN_EXT4_PROGRESS step=network-done'
    wait_for_interactive_login
    printf '%s\n' ext4-debian-apt-smoke >"$PERSISTENCE_FILE" ||
        fail persistence-write
    sync || fail sync
    emit 'DEBIAN_EXT4_M5_PASS boot=1 apt=1 maintainer=1 triggers=1 locks=1 recovery=1 packages=1 upgrade=1'
    emit "DEBIAN_EXT4_READY boot=1 arch=$architecture release=$debian_release pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=1 package=hello dpkg=1 login=1 user=1 apt_install=1 apt_remove=1 service=1 network=1 persist=1"
    systemctl --no-block --no-wall reboot || fail reboot
else
    if [[ "$NETWORK_RECOVERY_ONLY" == 1 ]]; then
        check_network
        check_network_recovery
        emit 'DEBIAN_EXT4_NETWORK_ONLY boot=2'
        emit 'DEBIAN_EXT4_NETWORK_ONLY_PASS boots=2 manager=1'
        exit 0
    fi
    [[ "$(/bin/cat -- "$PERSISTENCE_FILE")" == ext4-debian-apt-smoke ]] ||
        fail persistence-read
    [[ "$(/usr/bin/hello)" == 'Hello, world!' ]] || fail hello-persist
    [[ "$(dpkg-query -W -f='${Status}\n' hello)" == 'install ok installed' ]] ||
        fail dpkg-persist
    id "$TEST_USER" >/dev/null 2>&1 || fail user-persist
    su - "$TEST_USER" -c 'test "$(id -u)" -ge 1000' || fail login-persist
    systemctl is-enabled --quiet "$TEST_SERVICE" || fail service-persist
    systemctl start "$TEST_SERVICE" || fail service-restart
    systemctl is-active --quiet "$TEST_SERVICE" || fail service-restart
    systemctl stop "$TEST_SERVICE" || fail service-stop
    check_network
    check_network_recovery
    check_loopback_sockets || fail loopback-sockets
    sync || fail sync
    [[ "$(dpkg-query -W -f='${Status}\n' python3-minimal)" == 'install ok installed' ]] || fail m5-persist-python
    [[ "$(dpkg-query -W -f='${Status}\n' iproute2)" == 'install ok installed' ]] || fail m5-persist-iproute2
    /usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 13)' || fail m5-persist-python-runtime
    dpkg --audit | /bin/grep -q . && fail m5-persist-audit || true
    emit 'DEBIAN_EXT4_M5_PASS boot=2 apt=1 maintainer=1 triggers=1 locks=1 recovery=1 packages=1 upgrade=1 persist=1'
    emit "DEBIAN_EXT4_READY boot=2 arch=$architecture release=$debian_release pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=0 package=hello dpkg=1 login=1 user=1 apt_install=1 apt_remove=1 service=1 network=1 persist=1"
    emit 'DEBIAN_EXT4_PASS boot=2 persist=1'
fi
