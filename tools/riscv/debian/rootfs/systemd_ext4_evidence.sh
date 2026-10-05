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

check_login_and_user() {
    if ! id "$TEST_USER" >/dev/null 2>&1; then
        useradd --create-home --shell /bin/bash "$TEST_USER" || fail user-create
        printf '%s\n' "$TEST_USER:asterinas" | chpasswd || fail user-password
    fi
    local uid
    uid="$(id -u "$TEST_USER")" || fail user-identity
    [[ "$uid" -ge 1000 ]] || fail user-identity
    su - "$TEST_USER" -c \
        'test "$(id -u)" -ge 1000 && test "$HOME" = /home/debian' ||
        fail login
}

configure_and_test_service() {
    cat >/etc/systemd/system/"$TEST_SERVICE" <<'EOF'
[Unit]
Description=Asterinas Debian user service

[Service]
Type=simple
ExecStart=/bin/sleep 30
EOF
    systemctl daemon-reload || fail service-reload
    systemctl enable "$TEST_SERVICE" || fail service-enable
    systemctl start "$TEST_SERVICE" || fail service-start
    systemctl is-active --quiet "$TEST_SERVICE" || fail service-active
    systemctl stop "$TEST_SERVICE" || fail service-stop
    if systemctl is-active --quiet "$TEST_SERVICE"; then
        fail service-inactive
    fi
}

check_network() {
    /usr/bin/curl --fail --silent --show-error --location --max-time 20 \
        http://deb.debian.org/debian/README | /bin/grep -q Debian ||
        fail network-request
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
    # The kernel gives the guest its fixed 10.0.2.15/24 address and 10.0.2.2
    # gateway. QEMU's user-mode DNS proxy is the stable resolver for this gate.
    printf 'nameserver 10.0.2.3\n' > /etc/resolv.conf || fail resolver
    apt-get update || fail apt-update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends hello ||
        fail apt-install
    [[ "$(/usr/bin/hello)" == 'Hello, world!' ]] || fail hello
    [[ "$(dpkg-query -W -f='${Status}\n' hello)" == 'install ok installed' ]] ||
        fail dpkg
    DEBIAN_FRONTEND=noninteractive apt-get remove -y hello || fail apt-remove
    if dpkg-query -W -f='${Status}\n' hello >/dev/null 2>&1; then
        fail apt-remove
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends hello ||
        fail apt-reinstall
    [[ "$(/usr/bin/hello)" == 'Hello, world!' ]] || fail hello-reinstall
    # These locks are runtime artefacts. Removing them leaves a clean ext4
    # image for the post-boot checker while preserving apt lists and dpkg state.
    rm -f -- \
        /var/cache/apt/archives/lock \
        /var/lib/apt/lists/lock \
        /var/lib/dpkg/lock \
        /var/lib/dpkg/lock-frontend
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
    check_network
    wait_for_interactive_login
    printf '%s\n' ext4-debian-apt-smoke >"$PERSISTENCE_FILE" ||
        fail persistence-write
    sync || fail sync
    emit "DEBIAN_EXT4_READY boot=1 arch=$architecture release=$debian_release pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=1 package=hello dpkg=1 login=1 user=1 apt_install=1 apt_remove=1 service=1 network=1 persist=1"
    systemctl --no-block --no-wall reboot || fail reboot
else
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
    sync || fail sync
    emit "DEBIAN_EXT4_READY boot=2 arch=$architecture release=$debian_release pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=0 package=hello dpkg=1 login=1 user=1 apt_install=1 apt_remove=1 service=1 network=1 persist=1"
    emit 'DEBIAN_EXT4_PASS boot=2 persist=1'
fi
