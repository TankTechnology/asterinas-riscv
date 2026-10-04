#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

umask 022

readonly STATE_DIRECTORY="${ASTERINAS_EXT4_STATE_DIRECTORY:-/var/lib/asterinas-debian-ext4}"
readonly CONSOLE="${ASTERINAS_EXT4_CONSOLE:-/dev/console}"
readonly DEBIAN_VERSION_FILE="${ASTERINAS_EXT4_DEBIAN_VERSION_FILE:-/etc/debian_version}"
readonly COUNTER="$STATE_DIRECTORY/boot-count"
readonly PERSISTENCE_FILE="$STATE_DIRECTORY/persistence"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
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

check_pid1_and_root() {
    local pid1 root_filesystem
    pid1="$(tr -d '[:space:]' </proc/1/comm)" || fail pid1
    [[ "$pid1" == systemd ]] || fail pid1
    root_filesystem="$(stat -f -c '%T' /)" || fail root-filesystem
    [[ "$root_filesystem" == ext4 ]] || fail root-filesystem
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
    install_hello
    printf '%s\n' ext4-debian-apt-smoke >"$PERSISTENCE_FILE" ||
        fail persistence-write
    sync || fail sync
    emit "DEBIAN_EXT4_READY boot=1 arch=$architecture release=$debian_release pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=1 package=hello dpkg=1 network=1 persist=1"
    systemctl --no-block --no-wall reboot || fail reboot
else
    [[ "$(/bin/cat -- "$PERSISTENCE_FILE")" == ext4-debian-apt-smoke ]] ||
        fail persistence-read
    [[ "$(/usr/bin/hello)" == 'Hello, world!' ]] || fail hello-persist
    [[ "$(dpkg-query -W -f='${Status}\n' hello)" == 'install ok installed' ]] ||
        fail dpkg-persist
    sync || fail sync
    emit "DEBIAN_EXT4_READY boot=2 arch=$architecture release=$debian_release pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=0 package=hello dpkg=1 network=1 persist=1"
    emit 'DEBIAN_EXT4_PASS boot=2 persist=1'
fi
