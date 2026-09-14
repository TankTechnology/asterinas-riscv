#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly provider="${ASTERINAS_DISPLAY_PROVIDER:-fbdev}"
readonly provider_root="${ASTERINAS_DISPLAY_PROVIDER_ROOT:-/etc/asterinas/display-providers}"

case "$provider" in
    fbdev | drm) ;;
    *) exit 64 ;;
esac

readonly config_directory="$provider_root/$provider/xorg.conf.d"
[[ -d "$config_directory" && ! -L "$config_directory" ]] || exit 65
[[ -f "$config_directory/20-asterinas.conf" && \
    ! -L "$config_directory/20-asterinas.conf" ]] || exit 65

printf '%s\n' "$config_directory"
