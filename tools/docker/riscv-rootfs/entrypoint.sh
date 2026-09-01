#!/usr/bin/env bash

# SPDX-License-Identifier: MPL-2.0

set -Eeuo pipefail

readonly BINFMT_DIR=/proc/sys/fs/binfmt_misc
readonly BINFMT_REGISTRATION="$BINFMT_DIR/qemu-riscv64"
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

mount_binfmt_misc() {
    if [[ -r "$BINFMT_DIR/status" ]]; then
        return
    fi

    require_command mount
    mount -t binfmt_misc binfmt_misc "$BINFMT_DIR" 2>/dev/null ||
        die "cannot mount binfmt_misc; run the container with --privileged"
    [[ -r "$BINFMT_DIR/status" ]] ||
        die "binfmt_misc mount did not expose its status file"
}

registration_enabled() {
    [[ -r "$BINFMT_REGISTRATION" ]] &&
        grep -qx enabled "$BINFMT_REGISTRATION"
}

ensure_binfmt_registration() {
    require_command update-binfmts

    if ! registration_enabled; then
        update-binfmts --enable qemu-riscv64 >/dev/null 2>&1 ||
            die "cannot enable qemu-riscv64 binfmt registration"
    fi
}

verify_binfmt_registration() {
    local interpreter

    registration_enabled ||
        die "qemu-riscv64 binfmt registration is not enabled"
    grep -q '^flags:.*F' "$BINFMT_REGISTRATION" ||
        die "qemu-riscv64 binfmt registration lacks fix-binary (F) semantics"

    interpreter="$(awk '$1 == "interpreter" { print $2; exit }' "$BINFMT_REGISTRATION")"
    [[ -n "$interpreter" ]] ||
        die "qemu-riscv64 binfmt registration has no interpreter"
    [[ "$interpreter" == *qemu* || "$interpreter" == *riscv64* ]] ||
        die "qemu-riscv64 binfmt interpreter is not a RISC-V QEMU binary: $interpreter"
    [[ -x "$interpreter" ]] ||
        die "qemu-riscv64 binfmt interpreter is not executable: $interpreter"
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
        debootstrap qemu-riscv64-static gpgv dpkg-query mke2fs dumpe2fs \
        debugfs sha256sum curl qemu-system-riscv64; do
        require_command "$command_name"
    done
}

verify_environment() {
    [[ "$(uname -m)" != riscv64 ]] ||
        die "the builder must run on a non-RISC-V host with a QEMU binfmt boundary"
    verify_tools
    verify_keyring
    mount_binfmt_misc
    ensure_binfmt_registration
    verify_binfmt_registration

    local interpreter
    interpreter="$(awk '$1 == "interpreter" { print $2; exit }' "$BINFMT_REGISTRATION")"
    printf 'ASTERINAS_RISCV_ROOTFS_ENV_PASS arch=%s qemu=%s binfmt=enabled,F keyring=%s@%s\n' \
        "$(uname -m)" \
        "$(qemu-riscv64-static --version | head -n 1)" \
        "$DEBIAN_KEYRING" \
        "$(dpkg-query --show --showformat='${Version}' debian-archive-keyring)"
    printf 'ASTERINAS_RISCV_ROOTFS_BINFMT interpreter=%s\n' "$interpreter"
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
