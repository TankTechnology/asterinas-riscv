#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly provider_root="${ASTERINAS_DISPLAY_PROVIDER_ROOT:-/etc/asterinas/display-providers}"
readonly config_directory="$provider_root/fbdev/xorg.conf.d"

has_provider_config() {
    local directory="$1"
    [[ "$directory" == /* && "$directory" != *$'\n'* ]] || return 1
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    [[ -f "$directory/20-asterinas.conf" && \
        ! -L "$directory/20-asterinas.conf" ]]
}

if has_provider_config "$config_directory"; then
    printf '%s\n' "$config_directory"
else
    exit 65
fi
