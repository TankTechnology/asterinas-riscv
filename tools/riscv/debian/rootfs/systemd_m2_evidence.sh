#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

umask 077

readonly STATE_DIRECTORY="${ASTERINAS_M2_STATE_DIRECTORY:-/var/lib/asterinas-debian-m2}"
readonly CONSOLE="${ASTERINAS_M2_CONSOLE:-/dev/console}"
readonly DEBIAN_VERSION_FILE="${ASTERINAS_M2_DEBIAN_VERSION_FILE:-/etc/debian_version}"
readonly COUNTER="$STATE_DIRECTORY/boot-count"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_SYSTEMD_M2_FAIL reason=$1"
    exit 1
}

architecture="$(uname -m)" || fail architecture
[[ "$architecture" == riscv64 ]] || fail architecture

debian_release="$(/bin/cat -- "$DEBIAN_VERSION_FILE")" || fail debian-release
[[ "$debian_release" =~ ^13\.(0|[1-9][0-9]*)$ ]] || fail debian-release

filesystem_type="$(stat -f -c '%T' /)" || fail root-filesystem
[[ "$filesystem_type" == ext2/ext3 ]] || fail root-filesystem

tmp_filesystem_type="$(stat -f -c '%T' /tmp)" || fail tmp-filesystem
[[ "$tmp_filesystem_type" == tmpfs ]] || fail tmp-filesystem

package_versions="$(
    dpkg-query -W -f='${Package}\t${Version}\n' systemd systemd-sysv
)" || fail systemd-packages
[[ "$package_versions" == *$'systemd\t'* ]] || fail systemd-packages
[[ "$package_versions" == *$'systemd-sysv\t'* ]] || fail systemd-packages

install -d -m 0755 -- "$STATE_DIRECTORY" || fail state-directory
if [[ -e "$COUNTER" ]]; then
    current="$(/bin/cat -- "$COUNTER")" || fail boot-count-read
    [[ "$current" == 1 ]] || fail invalid-boot-count
else
    current=0
fi
next=$((current + 1))
temporary="$STATE_DIRECTORY/.boot-count.$$"
printf '%s\n' "$next" >"$temporary" || fail boot-count-write
chmod 0644 "$temporary" || fail boot-count-write
mv -f -- "$temporary" "$COUNTER" || fail boot-count-write
sync || fail sync

emit "DEBIAN_SYSTEMD_M2_TMPFS boot=$next"
if ((next == 1)); then
    systemctl start systemd-logind.service || fail logind-service
    systemctl is-active --quiet systemd-logind.service || fail logind-service
    emit "DEBIAN_SYSTEMD_M2_LOGIND boot=1 state=active"
fi
emit "DEBIAN_SYSTEMD_M2_READY boot=$next arch=$architecture release=$debian_release"
if ((next == 1)); then
    reboot -f || fail reboot
else
    emit "DEBIAN_SYSTEMD_M2_PASS boot=2"
fi
