#!/usr/bin/env bash

# SPDX-License-Identifier: MPL-2.0

set -Eeuo pipefail

readonly DEBIAN_KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg
readonly MINIMUM_DEBIAN_KEYRING_VERSION=2025.1

die() {
    printf 'riscv-rootfs-builder: %s\n' "$*" >&2
    exit 1
}

require_command() {
    local command_name="$1"
    command -v "$command_name" >/dev/null 2>&1 ||
        die "required command is missing: $command_name"
}

verify_binfmt_registration() {
    local binfmt_dir registration interpreter

    binfmt_dir="${ASTERINAS_BINFMT_ROOT:-/proc/sys/fs/binfmt_misc}"
    registration="$binfmt_dir/qemu-riscv64"

    [[ -r "$binfmt_dir/status" ]] && grep -qx enabled "$binfmt_dir/status" ||
        die "RISC-V binfmt_misc is not enabled in the audited boundary"
    [[ -r "$registration" ]] && grep -qx enabled "$registration" ||
        die "qemu-riscv64 binfmt registration is not enabled"
    grep -q '^flags:.*F' "$registration" ||
        die "qemu-riscv64 binfmt registration lacks fix-binary (F) semantics"

    interpreter="$(awk '$1 == "interpreter" { print $2; exit }' "$registration")"
    [[ -n "$interpreter" ]] ||
        die "qemu-riscv64 binfmt registration has no interpreter"
    [[ "$interpreter" == *qemu* || "$interpreter" == *riscv64* ]] ||
        die "qemu-riscv64 binfmt interpreter is not a RISC-V QEMU binary: $interpreter"
    [[ -x "$interpreter" ]] ||
        die "qemu-riscv64 binfmt interpreter is not executable: $interpreter"
    printf '%s\n' "$interpreter"
}

verify_keyring() {
    local installed_version owner mode permissions

    [[ -f "$DEBIAN_KEYRING" ]] ||
        die "Debian archive keyring is missing: $DEBIAN_KEYRING"
    owner="$(stat -Lc '%u' "$DEBIAN_KEYRING")"
    mode="$(stat -Lc '%a' "$DEBIAN_KEYRING")"
    permissions="${mode: -3}"
    [[ "$owner" == 0 ]] ||
        die "Debian archive keyring is not root-owned: $DEBIAN_KEYRING"
    [[ "${permissions:1:1}" != [2367] && "${permissions:2:1}" != [2367] ]] ||
        die "Debian archive keyring is writable by group or other: $DEBIAN_KEYRING"

    installed_version="$(dpkg-query --show \
        --showformat='${Version}' debian-archive-keyring 2>/dev/null)" ||
        die "Debian archive keyring package metadata is missing"
    dpkg --compare-versions "$installed_version" ge "$MINIMUM_DEBIAN_KEYRING_VERSION" ||
        die "Debian archive keyring is stale: $installed_version < $MINIMUM_DEBIAN_KEYRING_VERSION"
}

verify_tools() {
    local command_name
    for command_name in \
        debootstrap proot qemu-riscv64-static gpgv dpkg-query mke2fs dumpe2fs \
        debugfs sha256sum curl qemu-system-riscv64; do
        require_command "$command_name"
    done
}

verify_environment() {
    local execution_mode interpreter

    [[ "$(uname -m)" != riscv64 ]] ||
        die "the builder must run on a non-RISC-V host with a QEMU execution boundary"
    verify_tools
    verify_keyring

    export ASTERINAS_EXPLICIT_QEMU="${ASTERINAS_EXPLICIT_QEMU:-1}"
    case "$ASTERINAS_EXPLICIT_QEMU" in
        1)
            execution_mode=explicit-proot
            interpreter="$(command -v qemu-riscv64-static)"
            ;;
        0)
            interpreter="$(verify_binfmt_registration)"
            execution_mode=audited-binfmt
            ;;
        *)
            die "ASTERINAS_EXPLICIT_QEMU must be 0 or 1"
            ;;
    esac

    printf 'ASTERINAS_RISCV_ROOTFS_ENV_PASS arch=%s qemu=%s execution=%s keyring=%s@%s\n' \
        "$(uname -m)" \
        "$(qemu-riscv64-static --version | head -n 1)" \
        "$execution_mode" \
        "$DEBIAN_KEYRING" \
        "$(dpkg-query --show --showformat='${Version}' debian-archive-keyring)"
    printf 'ASTERINAS_RISCV_ROOTFS_EXECUTION mode=%s interpreter=%s host_binfmt=unchanged\n' \
        "$execution_mode" "$interpreter"
}

main() {
    verify_environment
    if (($# > 0)) && [[ "$1" == --check ]]; then
        (($# == 1)) || die "--check cannot be combined with a command"
        return 0
    fi

    if (($# == 0)); then
        set -- /bin/bash
    fi
    exec "$@"
}

main "$@"
