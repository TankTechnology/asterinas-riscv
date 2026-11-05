#!/usr/bin/env bash

# SPDX-License-Identifier: MPL-2.0
#
# Shared shell helpers for the repository's host-side scripts.
#
# Source this file; do not execute it. The prologue deliberately uses only
# shell builtins, because some callers run with a deliberately empty PATH
# (`prepare_qemu_uboot_booti.sh --check-tools`):
#
#     _shell_self_dir=${BASH_SOURCE[0]%/*}
#     if [[ ${_shell_self_dir} == "${BASH_SOURCE[0]}" ]]; then _shell_self_dir=.; fi
#     source "${_shell_self_dir}/../lib/shell.sh"
#     unset _shell_self_dir
#     REPO_ROOT="$(repo_root)"
#
# `repo_root` resolves from this library's own location (`tools/lib`), so a
# script at any depth gets the same answer and a new script never has to count
# `..` segments again.

if [[ -z "${_ASTERINAS_SHELL_LIB_DIR:-}" ]]; then
    _asterinas_self_dir=${BASH_SOURCE[0]%/*}
    if [[ ${_asterinas_self_dir} == "${BASH_SOURCE[0]}" ]]; then
        _asterinas_self_dir=.
    fi
    _ASTERINAS_SHELL_LIB_DIR="$(cd -- "${_asterinas_self_dir}" && pwd -P)"
    unset _asterinas_self_dir
fi

# Prints the repository root, one absolute path and a newline.
repo_root() {
    (cd -- "${_ASTERINAS_SHELL_LIB_DIR}/../.." && pwd -P)
}

# Prints an error to stderr and exits with status 1.
#
# Usage: die "message"
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# Fails unless every named command is available on PATH.
#
# Usage: require_cmd gcc cpio
require_cmd() {
    local command_name
    for command_name in "$@"; do
        if ! command -v -- "${command_name}" >/dev/null 2>&1; then
            die "required command not found: ${command_name}"
        fi
    done
}

# Prints a step line to stderr.
#
# Usage: log_step "building the initramfs"
log_step() {
    printf '==> %s\n' "$*" >&2
}
